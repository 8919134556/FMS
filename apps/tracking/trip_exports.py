"""PDF / Excel rendering for the Trip Report downloads.

Pure presentation: every number comes from the report objects built by
apps.tracking.trip_analytics (which in turn reads the same trips and GPS
history the Trip Report page shows). Nothing here queries the database or
computes a metric of its own.

    PDF   reportlab (vector charts via reportlab.graphics — crisp at any zoom,
          no headless browser); A4 landscape, header/footer + "Page X of Y".
    Excel openpyxl in write-only (streaming) mode so a large trip's GPS History
          sheet doesn't balloon memory; native Excel charts that reference
          the sheet data, typed cells (real dates/durations/numbers), filters,
          frozen headers and SUBTOTAL totals rows that follow the filter.
    Map   the trip's real GPS fixes drawn over OpenStreetMap tiles (Web
          Mercator), fetched server-side with a short timeout and cached;
          if tiles can't be fetched the same route is drawn on a plain
          graticule — the path is always the real one, only the backdrop
          degrades. Configure with settings.TRIP_REPORT_MAP_TILE_URL.

Two report types, deliberately different in scope:
    Fleet Trip Report  (period_report_*)  summary, vehicle/day analysis, trip
                       details, stoppages, charts — NO per-reading GPS history.
    Trip Analysis      (trip_report_*)    one trip: details, analysis, charts,
                       route map and its COMPLETE GPS history.
"""

import concurrent.futures
import datetime
import io
import math
import os
import urllib.request

from django.conf import settings
from django.core.cache import cache

from apps.tracking import trip_analytics as ta

# ---------------------------------------------------------------------------
# Formatting (shared with trip_analytics' plain-language findings)
# ---------------------------------------------------------------------------

DASH = "—"


def fmt_km(value, places=1):
    return f"{value:,.{places}f} km" if value is not None else DASH


def fmt_speed(value):
    return f"{value:,.1f} km/h" if value is not None else DASH


def fmt_duration(seconds):
    if seconds is None:
        return DASH
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"


def fmt_dt(value, tz, pattern="%d %b %Y %H:%M:%S"):
    return value.astimezone(tz).strftime(pattern) if value else DASH


def fmt_coord(lat, lon):
    return f"{float(lat):.6f}, {float(lon):.6f}" if lat is not None and lon is not None else DASH


def fmt_pct(value):
    return f"{value:.1f}%" if value is not None else DASH


def status_label(status):
    return "In progress" if status == "ACTIVE" else "Completed"


# Palette — the app's design tokens (static/css/design-tokens.css). States are
# fixed categorical slots (never re-assigned by rank); magnitudes use a
# single-hue ramp.
INK = "#0f172a"
INK_2 = "#475569"
INK_3 = "#94a3b8"
RULE = "#e2e8f0"
SURFACE = "#f8fafc"
PRIMARY = "#2563eb"
STATE_COLORS = {"moving": "#2563eb", "idle": "#eb6834", "engine_off": "#94a3b8", "no_data": "#e2e8f0"}
START_COLOR = "#16a34a"
END_COLOR = "#dc2626"
SEQ_RAMP = ["#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a"]


# ---------------------------------------------------------------------------
# Route map (PNG) — real lat/lon fixes on Web Mercator
# ---------------------------------------------------------------------------

TILE_SIZE = 256
MAX_TILES = 48


def _project(lat, lon, zoom):
    lat = max(min(float(lat), 85.05112878), -85.05112878)
    scale = TILE_SIZE * (2 ** zoom)
    x = (float(lon) + 180.0) / 360.0 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def _fetch_tile(url_template, z, x, y):
    n = 2 ** z
    url = url_template.format(z=z, x=x % n, y=y, s="a")
    key = f"trip-report-tile:{url}"
    try:
        cached = cache.get(key)
    except Exception:  # cache backend down — just fetch
        cached = None
    if cached:
        return cached
    request = urllib.request.Request(url, headers={
        "User-Agent": getattr(settings, "TRIP_REPORT_MAP_USER_AGENT", "FMS-TripReport/1.0"),
        "Referer": getattr(settings, "TRIP_REPORT_MAP_REFERER", "") or "https://localhost/",
    })
    with urllib.request.urlopen(request, timeout=getattr(settings, "TRIP_REPORT_MAP_TILE_TIMEOUT", 5)) as response:
        data = response.read()
    try:
        cache.set(key, data, 7 * 24 * 3600)
    except Exception:
        pass
    return data


def render_route_map(points, stops=(), *, width=1400, height=760):
    """PNG bytes of the trip route, or ``None`` if the trip has no valid fix.
    Returns ``(png_bytes, basemap_ok)``."""
    from PIL import Image, ImageDraw, ImageFont

    fixes = [(p.latitude, p.longitude) for p in points if p.has_fix]
    if not fixes:
        return None, False

    pad = 70
    lats = [float(a) for a, _ in fixes]
    lons = [float(b) for _, b in fixes]
    zoom = 16
    if len(set(fixes)) > 1:
        for z in range(17, 1, -1):
            x0, y0 = _project(max(lats), min(lons), z)
            x1, y1 = _project(min(lats), max(lons), z)
            if (x1 - x0) <= width - 2 * pad and (y1 - y0) <= height - 2 * pad:
                zoom = z
                break
        else:
            zoom = 2
    cx0, cy0 = _project(max(lats), min(lons), zoom)
    cx1, cy1 = _project(min(lats), max(lons), zoom)
    origin_x = (cx0 + cx1) / 2 - width / 2
    origin_y = (cy0 + cy1) / 2 - height / 2

    base = Image.new("RGB", (width, height), "#eef2f6")
    basemap_ok = False
    url_template = getattr(settings, "TRIP_REPORT_MAP_TILE_URL", "")
    if url_template:
        tx0, ty0 = int(origin_x // TILE_SIZE), int(origin_y // TILE_SIZE)
        tx1, ty1 = int((origin_x + width) // TILE_SIZE), int((origin_y + height) // TILE_SIZE)
        n = 2 ** zoom
        tiles = [(x, y) for x in range(tx0, tx1 + 1) for y in range(max(ty0, 0), min(ty1, n - 1) + 1)]
        if len(tiles) <= MAX_TILES:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    futures = {pool.submit(_fetch_tile, url_template, zoom, x, y): (x, y) for x, y in tiles}
                    for future in concurrent.futures.as_completed(futures, timeout=20):
                        x, y = futures[future]
                        tile = Image.open(io.BytesIO(future.result())).convert("RGB")
                        base.paste(tile, (int(x * TILE_SIZE - origin_x), int(y * TILE_SIZE - origin_y)))
                basemap_ok = True
            except Exception:
                base = Image.new("RGB", (width, height), "#eef2f6")
        if basemap_ok:
            # Soften the basemap so the route reads first.
            base = Image.blend(base, Image.new("RGB", base.size, "#ffffff"), 0.18)

    if not basemap_ok:
        _draw_graticule(base, zoom, origin_x, origin_y)

    # Route + markers drawn at 2x and downsampled — Pillow lines aren't anti-aliased.
    ss = 2
    overlay = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def to_px(lat, lon):
        x, y = _project(lat, lon, zoom)
        return (x - origin_x) * ss, (y - origin_y) * ss

    path = [to_px(a, b) for a, b in fixes]
    if len(path) > 1:
        draw.line(path, fill=(255, 255, 255, 235), width=11 * ss, joint="curve")
        draw.line(path, fill=PRIMARY, width=6 * ss, joint="curve")

    def font(size):
        try:
            return ImageFont.truetype(_font_file(bold=True) or "DejaVuSans-Bold.ttf", size * ss)
        except Exception:
            return ImageFont.load_default(size=size * ss)

    def marker(xy, color, label, radius=15):
        x, y = xy
        r = radius * ss
        draw.ellipse((x - r - 3 * ss, y - r - 3 * ss, x + r + 3 * ss, y + r + 3 * ss), fill="white")
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        draw.text((x, y), label, fill="white", font=font(13 if len(label) < 3 else 10), anchor="mm")

    ranked = sorted(stops, key=lambda s: s.duration_seconds, reverse=True)[:15]
    for number, stop in sorted(((i + 1, s) for i, s in enumerate(ranked)), key=lambda t: t[0], reverse=True):
        if stop.latitude is not None and stop.longitude is not None:
            marker(to_px(stop.latitude, stop.longitude), STATE_COLORS["idle"], str(number), radius=11)
    marker(path[-1], END_COLOR, "B")
    marker(path[0], START_COLOR, "A")

    overlay = overlay.resize((width, height), Image.LANCZOS)
    base = base.convert("RGBA")
    base.alpha_composite(overlay)

    _draw_scale_bar(base, zoom, (min(lats) + max(lats)) / 2, font)
    attribution = "© OpenStreetMap contributors" if basemap_ok else "Basemap unavailable — route drawn from GPS fixes"
    ad = ImageDraw.Draw(base)
    f = ImageFont.truetype(_font_file() or "DejaVuSans.ttf", 15) if _font_file() else ImageFont.load_default(size=15)
    tw = ad.textlength(attribution, font=f)
    ad.rectangle((width - tw - 16, height - 26, width, height), fill=(255, 255, 255, 220))
    ad.text((width - tw - 8, height - 23), attribution, fill="#334155", font=f)

    out = io.BytesIO()
    base.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue(), basemap_ok


def _draw_graticule(image, zoom, origin_x, origin_y):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    w, h = image.size
    step = TILE_SIZE / 2
    offset_x = -(origin_x % step)
    offset_y = -(origin_y % step)
    x = offset_x
    while x < w:
        draw.line((x, 0, x, h), fill="#dbe3ec", width=1)
        x += step
    y = offset_y
    while y < h:
        draw.line((0, y, w, y), fill="#dbe3ec", width=1)
        y += step


def _draw_scale_bar(image, zoom, mid_lat, font):
    from PIL import ImageDraw

    metres_per_px = 156543.03392 * math.cos(math.radians(mid_lat)) / (2 ** zoom)
    target = metres_per_px * 180
    nice = min((d for d in (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000,
                            500000, 1000000) if d >= target * 0.5), default=1000000)
    px = nice / metres_per_px
    label = f"{nice // 1000:g} km" if nice >= 1000 else f"{nice} m"
    draw = ImageDraw.Draw(image)
    x0, y0 = 20, image.size[1] - 34
    draw.rectangle((x0 - 8, y0 - 22, x0 + px + 60, y0 + 14), fill=(255, 255, 255, 220))
    draw.line((x0, y0, x0 + px, y0), fill=INK, width=3)
    draw.line((x0, y0 - 7, x0, y0 + 7), fill=INK, width=3)
    draw.line((x0 + px, y0 - 7, x0 + px, y0 + 7), fill=INK, width=3)
    try:
        from PIL import ImageFont

        f = ImageFont.truetype(_font_file(bold=True) or "DejaVuSans-Bold.ttf", 15)
    except Exception:
        from PIL import ImageFont

        f = ImageFont.load_default(size=15)
    draw.text((x0 + px + 8, y0 - 9), label, fill=INK, font=f)


# ---------------------------------------------------------------------------
# Fonts — a Unicode TTF so addresses/symbols render; Helvetica as last resort
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]


def _font_file(bold=False):
    for regular, bold_path in _FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold_path):
            return bold_path if bold else regular
    return None


_PDF_FONTS = None


def _pdf_fonts():
    """(regular, bold, unicode_ok) — registered once per process."""
    global _PDF_FONTS
    if _PDF_FONTS is None:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        regular, bold = _font_file(), _font_file(bold=True)
        if regular and bold:
            try:
                pdfmetrics.registerFont(TTFont("FmsSans", regular))
                pdfmetrics.registerFont(TTFont("FmsSans-Bold", bold))
                pdfmetrics.registerFontFamily("FmsSans", normal="FmsSans", bold="FmsSans-Bold")
                _PDF_FONTS = ("FmsSans", "FmsSans-Bold", True)
            except Exception:
                _PDF_FONTS = ("Helvetica", "Helvetica-Bold", False)
        else:
            _PDF_FONTS = ("Helvetica", "Helvetica-Bold", False)
    return _PDF_FONTS


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


class _Pdf:
    """Shared PDF building blocks (styles, header/footer, cards, tables, charts)."""

    def __init__(self, meta):
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm

        self.meta = meta
        self.colors = colors
        self.mm = mm
        self.page_size = landscape(A4)
        self.margin = 14 * mm
        self.content_width = self.page_size[0] - 2 * self.margin
        self.font, self.font_bold, self.unicode_ok = _pdf_fonts()
        c = colors.HexColor
        self.s = {
            "h1": ParagraphStyle("h1", fontName=self.font_bold, fontSize=20, leading=24, textColor=c(INK)),
            "sub": ParagraphStyle("sub", fontName=self.font, fontSize=10.5, leading=14, textColor=c(INK_2)),
            "h2": ParagraphStyle("h2", fontName=self.font_bold, fontSize=13, leading=16, textColor=c(INK)),
            "body": ParagraphStyle("body", fontName=self.font, fontSize=8.5, leading=11.5, textColor=c(INK)),
            "note": ParagraphStyle("note", fontName=self.font, fontSize=7.5, leading=10, textColor=c(INK_2)),
            "cell": ParagraphStyle("cell", fontName=self.font, fontSize=7.2, leading=9, textColor=c(INK)),
            "cell_b": ParagraphStyle("cell_b", fontName=self.font_bold, fontSize=7.2, leading=9, textColor=c(INK)),
            "th": ParagraphStyle("th", fontName=self.font_bold, fontSize=7.2, leading=9, textColor=colors.white),
            "th_r": ParagraphStyle("th_r", fontName=self.font_bold, fontSize=7.2, leading=9, textColor=colors.white,
                                   alignment=2),
            "k_label": ParagraphStyle("k_label", fontName=self.font, fontSize=7.5, leading=9.5, textColor=c(INK_2)),
            "k_value": ParagraphStyle("k_value", fontName=self.font_bold, fontSize=14, leading=17, textColor=c(INK)),
            "k_detail": ParagraphStyle("k_detail", fontName=self.font, fontSize=7, leading=9, textColor=c(INK_2)),
        }

    # -- text --
    def t(self, text):
        """Escape for Paragraph markup; swap glyphs Helvetica can't draw."""
        text = "" if text is None else str(text)
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if not self.unicode_ok:
            text = text.replace("→", "->").replace("≥", ">=").replace("≤", "<=")
        return text

    def p(self, text, style="body"):
        from reportlab.platypus import Paragraph

        return Paragraph(self.t(text), self.s[style])

    def section(self, title, note=""):
        from reportlab.platypus import KeepTogether, Spacer, Table, TableStyle

        c = self.colors.HexColor
        bar = Table([[self.p(title, "h2")]], colWidths=[self.content_width])
        bar.setStyle(TableStyle([
            ("LINEBEFORE", (0, 0), (0, 0), 3, c(PRIMARY)),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        parts = [Spacer(1, 10), bar]
        if note:
            parts += [Spacer(1, 3), self.p(note, "note")]
        parts.append(Spacer(1, 6))
        return KeepTogether(parts)

    # -- KPI cards --
    def kpi_grid(self, cards, cols=4):
        """cards: [(label, value, detail)] — a grid of summary cards."""
        from reportlab.platypus import Table, TableStyle

        c = self.colors.HexColor
        gap = 6
        col_w = (self.content_width - gap * (cols - 1)) / cols
        cells = []
        for label, value, detail in cards:
            inner = [self.p(label, "k_label"), self.p(value, "k_value")]
            if detail:
                inner.append(self.p(detail, "k_detail"))
            card = Table([[x] for x in inner], colWidths=[col_w])
            card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), c(SURFACE)),
                ("BOX", (0, 0), (-1, -1), 0.6, c(RULE)),
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, c(PRIMARY)),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 7),
                ("TOPPADDING", (0, 1), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -2), 1),
            ]))
            cells.append(card)
        rows = []
        for i in range(0, len(cells), cols):
            row = cells[i:i + cols]
            row += [""] * (cols - len(row))
            spaced = []
            for j, cell in enumerate(row):
                spaced.append(cell)
                if j < cols - 1:
                    spaced.append("")
            rows.append(spaced)
        widths = []
        for j in range(cols):
            widths.append(col_w)
            if j < cols - 1:
                widths.append(gap)
        grid = Table(rows, colWidths=widths)
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), gap),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
        ]))
        return grid

    # -- data tables --
    def table(self, headers, rows, widths, *, align_right=(), bold_last=False, wrap=(), dense=False):
        """widths are fractions of the content width. ``wrap`` columns become
        Paragraphs (long addresses wrap instead of overflowing)."""
        from reportlab.platypus import LongTable, TableStyle

        c = self.colors.HexColor
        total = sum(widths)
        col_widths = [self.content_width * w / total for w in widths]
        align_right = set(align_right)
        data = [[self.p(h, "th_r" if j in align_right else "th") for j, h in enumerate(headers)]]
        for index, row in enumerate(rows):
            is_bold = bold_last and index == len(rows) - 1
            data.append([
                self.p(v, "cell_b" if is_bold else "cell") if (j in wrap or is_bold) else self.t(v if v is not None else DASH)
                for j, v in enumerate(row)
            ])
        table = LongTable(data, colWidths=col_widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), c(INK)),
            ("FONTNAME", (0, 1), (-1, -1), self.font),
            ("FONTSIZE", (0, 1), (-1, -1), 7.2),
            ("TEXTCOLOR", (0, 1), (-1, -1), c(INK)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, c(RULE)),
            ("TOPPADDING", (0, 0), (-1, -1), 1.8 if dense else 3.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8 if dense else 3.2),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        for i in range(1, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), c(SURFACE)))
        for j in align_right:
            style.append(("ALIGN", (j, 1), (j, -1), "RIGHT"))
        if bold_last and rows:
            style += [("BACKGROUND", (0, -1), (-1, -1), c("#e0e7ff")), ("LINEABOVE", (0, -1), (-1, -1), 0.8, c(INK))]
        table.setStyle(TableStyle(style))
        return table

    def key_value(self, pairs, cols=2):
        from reportlab.platypus import Table, TableStyle

        c = self.colors.HexColor
        rows = []
        for i in range(0, len(pairs), cols):
            row = []
            for label, value in pairs[i:i + cols]:
                row += [self.p(label, "k_label"), self.p(value, "cell_b")]
            row += ["", ""] * (cols - len(pairs[i:i + cols]))
            rows.append(row)
        unit = self.content_width / cols
        table = Table(rows, colWidths=[unit * 0.28, unit * 0.72] * cols)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, c(RULE)),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return table

    def insights_table(self, insights):
        rows = [[label, value, detail] for label, value, detail in insights]
        return self.table(["Finding", "Result", "Detail"], rows, [0.2, 0.2, 0.6], wrap=(0, 1, 2))

    # -- charts --
    def _chart_frame(self, title, width, height, subtitle=""):
        from reportlab.graphics.shapes import Drawing, String

        d = Drawing(width, height)
        d.add(String(0, height - 12, self.t(title), fontName=self.font_bold, fontSize=9.5, fillColor=self.colors.HexColor(INK)))
        if subtitle:
            d.add(String(0, height - 24, self.t(subtitle), fontName=self.font, fontSize=7, fillColor=self.colors.HexColor(INK_2)))
        return d

    def _style_axes(self, chart, *, value_axis, category_axis):
        c = self.colors.HexColor
        for axis in (value_axis, category_axis):
            axis.labels.fontName = self.font
            axis.labels.fontSize = 6.8
            axis.labels.fillColor = c(INK_2)
            axis.strokeColor = c(RULE)
            axis.strokeWidth = 0.6
        value_axis.visibleGrid = True
        value_axis.gridStrokeColor = c(RULE)
        value_axis.gridStrokeWidth = 0.4
        value_axis.visibleAxis = False
        value_axis.visibleTicks = False
        category_axis.visibleTicks = False

    def _thin_bars(self, chart, count, length):
        """Thin marks: a bar never wider than ~22pt nor more than 60% of its slot."""
        slot = length / max(count, 1)
        ratio = min(0.6, 22 / slot) if slot > 0 else 0.6
        chart.barWidth = ratio
        chart.groupSpacing = 1 - ratio
        chart.barSpacing = 0

    def _value_scale(self, axis, top_value, integer):
        if integer:
            step = max(1, math.ceil(top_value / 5)) if top_value > 0 else 1
            axis.valueMin = 0
            axis.valueStep = step
            axis.valueMax = step * (math.floor(top_value / step) + 1)
        else:
            axis.valueMin = 0
            axis.valueMax = top_value * 1.18 if top_value > 0 else 1

    def bar_chart(self, title, labels, values, *, width, height=190, subtitle="", horizontal=False,
                  value_format="{:,.0f}", colors_per_bar=None, color=PRIMARY, integer=False):
        from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart

        c = self.colors.HexColor
        d = self._chart_frame(title, width, height, subtitle)
        top = 30 if subtitle else 20
        values = [v or 0 for v in values]
        chart = HorizontalBarChart() if horizontal else VerticalBarChart()
        if horizontal:
            label_w = min(110, max(40, max((len(str(lbl)) for lbl in labels), default=4) * 4.4))
            chart.x, chart.y = label_w, 12
            chart.width, chart.height = width - label_w - 40, height - top - 18
        else:
            chart.x, chart.y = 34, 30
            chart.width, chart.height = width - 44, height - top - 36
        chart.data = [values]
        chart.categoryAxis.categoryNames = [self.t(str(lbl)) for lbl in labels]
        chart.bars.strokeColor = None
        chart.bars[0].fillColor = c(color)
        if colors_per_bar:
            for i, col in enumerate(colors_per_bar):
                chart.bars[(0, i)].fillColor = c(col)
        self._thin_bars(chart, len(values), chart.height if horizontal else chart.width)
        self._value_scale(chart.valueAxis, max(values) if values else 0, integer)
        chart.valueAxis.labelTextFormat = lambda v: value_format.format(v)
        self._style_axes(chart, value_axis=chart.valueAxis, category_axis=chart.categoryAxis)
        if not horizontal and len(labels) > 10:
            chart.categoryAxis.labels.angle = 45
            chart.categoryAxis.labels.boxAnchor = "ne"
            chart.categoryAxis.labels.dy = -2
        # Direct value labels only when few bars (no number on every point otherwise).
        if len(values) <= 14:
            chart.barLabelFormat = lambda v: value_format.format(v) if v else ""
            chart.barLabels.fontName = self.font
            chart.barLabels.fontSize = 6.5
            chart.barLabels.fillColor = c(INK_2)
            if horizontal:
                chart.barLabels.boxAnchor = "w"
                chart.barLabels.dx = 3
            else:
                chart.barLabels.boxAnchor = "s"
                chart.barLabels.dy = 2
        d.add(chart)
        return d

    def grouped_bar_chart(self, title, labels, series, *, width, height=200, subtitle="", value_format="{:,.1f}"):
        """series: [(name, values, color)] side by side per category on ONE
        value axis (same unit only), with a legend between title and plot."""
        from reportlab.graphics.charts.barcharts import VerticalBarChart
        from reportlab.graphics.charts.legends import Legend

        c = self.colors.HexColor
        d = self._chart_frame(title, width, height, subtitle)
        top = 30 if subtitle else 20
        chart = VerticalBarChart()
        chart.x, chart.y = 38, 30
        chart.width, chart.height = width - 48, height - top - 56
        chart.data = [[v or 0 for v in values] for _name, values, _color in series]
        chart.categoryAxis.categoryNames = [self.t(str(lbl)) for lbl in labels]
        chart.bars.strokeColor = c("#ffffff")
        chart.bars.strokeWidth = 1  # 2px surface gap between adjacent bars
        for i, (_name, _values, color) in enumerate(series):
            chart.bars[i].fillColor = c(color)
        slot = chart.width / max(len(labels), 1)
        bar = min(14, slot * 0.6 / max(len(series), 1))
        chart.barWidth = bar
        chart.barSpacing = 0
        chart.groupSpacing = max(slot - bar * len(series), 1)
        top_value = max((max(col) for col in chart.data if col), default=0)
        self._value_scale(chart.valueAxis, top_value, False)
        chart.valueAxis.labelTextFormat = lambda v: value_format.format(v)
        self._style_axes(chart, value_axis=chart.valueAxis, category_axis=chart.categoryAxis)
        if len(labels) > 10:
            chart.categoryAxis.labels.angle = 45
            chart.categoryAxis.labels.boxAnchor = "ne"
        d.add(chart)
        legend = Legend()
        legend.x, legend.y = chart.x, height - top - 16
        legend.alignment = "right"
        legend.columnMaximum = 1
        legend.deltax = 120
        legend.fontName = self.font
        legend.fontSize = 7
        legend.boxAnchor = "sw"
        legend.dxTextSpace = 4
        legend.colorNamePairs = [(c(color), self.t(name)) for name, _v, color in series]
        d.add(legend)
        return d

    def stacked_hbar(self, title, labels, series, *, width, height=None, subtitle="", value_format="{:,.1f}"):
        """series: [(name, values, color)] stacked per category, with a legend."""
        from reportlab.graphics.charts.barcharts import HorizontalBarChart
        from reportlab.graphics.charts.legends import Legend

        c = self.colors.HexColor
        height = height or max(150, 60 + 26 * len(labels))
        d = self._chart_frame(title, width, height, subtitle)
        top = 30 if subtitle else 20
        label_w = min(110, max(40, max((len(str(lbl)) for lbl in labels), default=4) * 4.4))
        chart = HorizontalBarChart()
        chart.x, chart.y = label_w, 16
        chart.width, chart.height = width - label_w - 16, height - top - 40
        chart.data = [[v or 0 for v in values] for _name, values, _color in series]
        chart.categoryAxis.categoryNames = [self.t(str(lbl)) for lbl in labels]
        chart.categoryAxis.style = "stacked"
        chart.bars.strokeColor = c("#ffffff")
        chart.bars.strokeWidth = 1
        for i, (_name, _values, color) in enumerate(series):
            chart.bars[i].fillColor = c(color)
        chart.valueAxis.valueMin = 0
        totals = [sum(col) for col in zip(*chart.data)] or [0]
        chart.valueAxis.valueMax = max(totals) * 1.08 if max(totals) > 0 else 1
        chart.valueAxis.labelTextFormat = lambda v: value_format.format(v)
        self._thin_bars(chart, len(labels), chart.height)
        self._style_axes(chart, value_axis=chart.valueAxis, category_axis=chart.categoryAxis)
        d.add(chart)
        # Legend sits between the title and the plot, clear of the axis labels.
        legend = Legend()
        legend.x, legend.y = label_w, height - top - 16
        legend.alignment = "right"
        legend.columnMaximum = 1
        legend.deltax = 110
        legend.fontName = self.font
        legend.fontSize = 7
        legend.boxAnchor = "sw"
        legend.dxTextSpace = 4
        legend.colorNamePairs = [(c(color), self.t(name)) for name, _v, color in series]
        d.add(legend)
        return d

    def line_chart(self, title, xs, ys, *, width, height=200, subtitle="", y_format="{:,.0f}", color=PRIMARY,
                   start_at=None, tz=None, reference=None):
        """xs: seconds from ``start_at``; x-axis labelled as local clock time."""
        from reportlab.graphics.charts.lineplots import LinePlot
        from reportlab.graphics.shapes import Line, String

        c = self.colors.HexColor
        d = self._chart_frame(title, width, height, subtitle)
        top = 30 if subtitle else 20
        pairs = [(x, y) for x, y in zip(xs, ys) if y is not None]
        if len(pairs) < 2:
            d.add(String(width / 2, height / 2, self.t("Not enough readings to plot"), fontName=self.font,
                         fontSize=8, fillColor=c(INK_3), textAnchor="middle"))
            return d
        plot = LinePlot()
        plot.x, plot.y = 38, 28
        plot.width, plot.height = width - 50, height - top - 36
        plot.data = [pairs]
        plot.lines[0].strokeColor = c(color)
        plot.lines[0].strokeWidth = 1.4
        max_y = max(y for _x, y in pairs)
        plot.yValueAxis.valueMin = 0
        plot.yValueAxis.valueMax = max_y * 1.12 if max_y > 0 else 1
        plot.yValueAxis.labelTextFormat = lambda v: y_format.format(v)
        plot.xValueAxis.valueMin = pairs[0][0]
        plot.xValueAxis.valueMax = pairs[-1][0]
        span = max(pairs[-1][0] - pairs[0][0], 1)
        plot.xValueAxis.valueSteps = [pairs[0][0] + span * i / 5 for i in range(6)]
        if start_at is not None:
            plot.xValueAxis.labelTextFormat = lambda v: (start_at + datetime.timedelta(seconds=v)).astimezone(tz).strftime("%H:%M")
        self._style_axes(plot, value_axis=plot.yValueAxis, category_axis=plot.xValueAxis)
        d.add(plot)
        if reference is not None and plot.yValueAxis.valueMax > reference:
            y = plot.y + plot.height * reference / plot.yValueAxis.valueMax
            d.add(Line(plot.x, y, plot.x + plot.width, y, strokeColor=c(INK_3), strokeWidth=0.6, strokeDashArray=[3, 2]))
            d.add(String(plot.x + plot.width - 2, y + 2, self.t(f"moving threshold {reference:g} km/h"),
                         fontName=self.font, fontSize=6, fillColor=c(INK_2), textAnchor="end"))
        return d

    def chart_grid(self, drawings, cols=2):
        from reportlab.platypus import Table, TableStyle

        rows = [drawings[i:i + cols] + [""] * (cols - len(drawings[i:i + cols])) for i in range(0, len(drawings), cols)]
        grid = Table(rows, colWidths=[self.content_width / cols] * cols)
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ]))
        return grid

    # -- document --
    def build(self, story):
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.platypus import SimpleDocTemplate

        meta, pdf = self.meta, self
        c = self.colors.HexColor

        class NumberedCanvas(rl_canvas.Canvas):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._pages = []

            def showPage(self):
                self._pages.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                total = len(self._pages)
                for state in self._pages:
                    self.__dict__.update(state)
                    self._decorate(total)
                    super().showPage()
                super().save()

            def _decorate(self, total):
                w, h = pdf.page_size
                m = pdf.margin
                self.setFillColor(c(INK))
                self.rect(0, h - 11 * pdf.mm, w, 11 * pdf.mm, stroke=0, fill=1)
                self.setFillColor(c("#ffffff"))
                self.setFont(pdf.font_bold, 9.5)
                self.drawString(m, h - 7 * pdf.mm, meta.company)
                self.setFont(pdf.font, 8)
                self.setFillColor(c("#cbd5e1"))
                right = f"{meta.title}  ·  {meta.subtitle}"
                self.drawRightString(w - m, h - 7 * pdf.mm, pdf.t(right) if not pdf.unicode_ok else right)
                self.setStrokeColor(c(RULE))
                self.setLineWidth(0.5)
                self.line(m, 10 * pdf.mm, w - m, 10 * pdf.mm)
                self.setFont(pdf.font, 7)
                self.setFillColor(c(INK_2))
                footer = (f"Generated {meta.generated_at:%d %b %Y %H:%M} ({meta.tz_name}) by {meta.app_name}"
                          "  ·  Computed from recorded GPS/telematics history")
                self.drawString(m, 6.5 * pdf.mm, footer)
                self.drawRightString(w - m, 6.5 * pdf.mm, f"Page {self._pageNumber} of {total}")

        out = io.BytesIO()
        doc = SimpleDocTemplate(
            out, pagesize=self.page_size, leftMargin=self.margin, rightMargin=self.margin,
            topMargin=17 * self.mm, bottomMargin=14 * self.mm,
            title=f"{meta.title} — {meta.subtitle}", author=meta.company, subject=meta.period_label,
            creator=meta.app_name,
        )
        doc.build(story, canvasmaker=NumberedCanvas)
        return out.getvalue()

    def title_block(self, extra_lines=()):
        from reportlab.platypus import Spacer

        m = self.meta
        story = [self.p(m.title, "h1"), Spacer(1, 3), self.p(m.subtitle, "sub"), Spacer(1, 6)]
        lines = [f"Period: {m.period_label}", f"Scope: {m.filters_label}",
                 f"Timezone: {m.tz_name}", f"Generated: {m.generated_at:%d %b %Y %H:%M:%S}"]
        story.append(self.p("   |   ".join(list(lines) + list(extra_lines)), "note"))
        story.append(Spacer(1, 10))
        return story


def _short(text, limit):
    """One-line location for dense PDF tables (the Excel export keeps it whole)."""
    if not text:
        return DASH
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,") + "…"


def _sample_rows(items, limit):
    """Evenly spaced sample that always keeps the first and last item."""
    if len(items) <= limit:
        return items, False
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)], True


def _state_of(analysis, points, index):
    if index + 1 >= len(points):
        return "Trip end" if analysis.trip.end_at else "Latest"
    seconds = (points[index + 1].timestamp - points[index].timestamp).total_seconds()
    if seconds <= 0:
        return ""
    return ta.STATE_LABELS[analysis._interval_state(points[index], seconds)]


def _methodology(pdf, meta):
    return (
        f"How figures are derived: ignition ON opens a candidate trip, confirmed only once the history shows "
        f"genuine movement (several readings at the vehicle's minimum speed, or the vehicle — or its odometer — "
        f"moving its minimum distance away); the trip starts where the vehicle departed, not at ignition ON. "
        f"It runs until ignition stays OFF for the vehicle's configured trip closure time ({meta.closure_label}) or "
        f"longer; shorter stops stay part of the trip, shown as “Stopped (engine off)”. "
        f"Distance is the device odometer difference, the same figure shown on the Trip Report page; the GPS path "
        f"length is used only when a trip has no odometer reading and is labelled “GPS”. Between consecutive "
        f"readings the vehicle is Moving at ≥ {meta.moving_threshold_kmh:g} km/h and Idling below it with the engine on. "
        f"Gaps of more than {int(ta.MAX_SAMPLE_GAP.total_seconds() // 60)} min without a reading count as “No data” "
        f"and are never guessed. Average speed = distance ÷ trip time; utilization = trip time ÷ time elapsed in the "
        f"period. Trips are assigned to the day they started."
    )


_FLEET_GPS_NOTE = (
    " This fleet report is a summary and analysis of trips; it does not list individual GPS readings. "
    "The complete GPS history of a trip is in that trip’s Trip Analysis Report (MAP → Download Trip Report)."
)


def period_report_pdf(report):
    from reportlab.platypus import PageBreak, Spacer

    meta, s = report.meta, report.summary
    pdf = _Pdf(meta)
    tz = meta.tz
    story = pdf.title_block()

    cards = [
        ("Vehicles", f"{s['total_vehicles']}", f"{s['vehicles_with_trips']} with trips"),
        ("Total trips", f"{s['total_trips']}", f"{s['completed_trips']} completed · {s['active_trips']} in progress"),
        ("Total distance", fmt_km(s["total_distance_km"]),
         f"avg {fmt_km(s['avg_trip_km'])} per trip" if s["avg_trip_km"] is not None else ""),
        ("Total travel time", fmt_duration(s["travel_seconds"]), f"avg {fmt_duration(s['avg_trip_seconds'])} per trip"),
        ("Driving (moving) time", fmt_duration(s["moving_seconds"]), ""),
        ("Idle time (engine on)", fmt_duration(s["idle_seconds"]), ""),
        ("Stopped in trip (engine off)", fmt_duration(s["engine_off_seconds"]), f"{s['stops']} stops ≥ 1 min"),
        ("Fleet utilization", fmt_pct(s["fleet_utilization_pct"]), "trip time ÷ elapsed period"),
        ("Average speed", fmt_speed(s["avg_speed_kmh"]), f"{fmt_speed(s['avg_moving_speed_kmh'])} while moving"),
        ("Maximum speed", fmt_speed(s["max_speed_kmh"]),
         s["max_speed_record"].vehicle.registration_number if s["max_speed_record"] else ""),
        ("Avg trip duration", fmt_duration(s["avg_trip_seconds"]), ""),
        ("GPS records", f"{s['gps_points']:,}", "in the period"),
    ]
    story += [pdf.section("1. Report Summary"), pdf.kpi_grid(cards, cols=4)]
    notes = []
    if s["earlier_active_trips"]:
        notes.append(f"{s['earlier_active_trips']} in-progress trip(s) started before the period and are included, "
                     "as on the Trip Report page.")
    if s["gps_distance_trips"]:
        notes.append(f"{s['gps_distance_trips']} trip(s) had no odometer reading. Their GPS path estimate "
                     f"({fmt_km(s['gps_estimated_km'])}) is shown on the trip row, marked “GPS est.”, but is not "
                     "included in distance totals — totals match the Trip Report page.")
    if s["no_distance_trips"]:
        notes.append(f"{s['no_distance_trips']} trip(s) have no distance data and are excluded from distance totals.")
    for note in notes:
        story.append(pdf.p("• " + note, "note"))

    story += [pdf.section("2. Key Findings", "Calculated only where the underlying data exists."),
              pdf.insights_table(report.insights)]

    # -- charts --
    half = pdf.content_width / 2 - 12
    charts = []
    days = report.day_rows
    if len(days) > 1:
        day_labels = [f"{d['date']:%d %b}" for d in days]
        charts.append(pdf.bar_chart("Trips per day", day_labels, [d["trips"] for d in days], width=half,
                                    subtitle="Trips by local start date", integer=True))
        charts.append(pdf.bar_chart("Distance per day (km)", day_labels, [d["distance_km"] for d in days],
                                    width=half, value_format="{:,.1f}", subtitle="Odometer distance of trips started that day"))
    rows_with_trips = [r for r in report.vehicle_rows if r["trips"]]
    if len(rows_with_trips) > 1:
        top = sorted(rows_with_trips, key=lambda r: r["distance_km"] or 0, reverse=True)[:12]
        charts.append(pdf.bar_chart("Distance by vehicle (km)", [r["registration_number"] for r in top],
                                    [r["distance_km"] for r in top], width=half, horizontal=True,
                                    height=max(150, 40 + 18 * len(top)), value_format="{:,.1f}",
                                    subtitle="Top vehicles by distance"))
        top_u = sorted(report.vehicle_rows, key=lambda r: r["utilization_pct"], reverse=True)[:12]
        charts.append(pdf.bar_chart("Vehicle utilization (%)", [r["registration_number"] for r in top_u],
                                    [r["utilization_pct"] for r in top_u], width=half, horizontal=True,
                                    height=max(150, 40 + 18 * len(top_u)), value_format="{:,.1f}",
                                    subtitle="Share of the period spent in trips"))
    if rows_with_trips:
        top_t = sorted(rows_with_trips, key=lambda r: r["travel_seconds"], reverse=True)[:12]
        charts.append(pdf.stacked_hbar(
            "Trip time by vehicle (hours)", [r["registration_number"] for r in top_t],
            [("Moving", [r["moving_seconds"] / 3600 for r in top_t], STATE_COLORS["moving"]),
             ("Idling", [r["idle_seconds"] / 3600 for r in top_t], STATE_COLORS["idle"]),
             ("Stopped (engine off)", [r["engine_off_seconds"] / 3600 for r in top_t], STATE_COLORS["engine_off"])],
            width=half, subtitle="How trip time was spent"))
    if any(report.band_seconds):
        total_band = sum(report.band_seconds)
        charts.append(pdf.bar_chart(
            "Speed profile (% of moving time)", report.band_labels,
            [v / total_band * 100 for v in report.band_seconds], width=half, value_format="{:,.1f}",
            colors_per_bar=SEQ_RAMP[2:2 + len(report.band_labels)] if len(report.band_labels) <= 6 else None,
            subtitle="Time spent in each speed band while moving"))
    if report.trips:
        charts.append(pdf.bar_chart("Trip starts by hour of day", [f"{h:02d}" for h in range(24)], report.hour_counts,
                                    width=half, subtitle=f"Local time ({meta.tz_name})", integer=True))
        charts.append(pdf.bar_chart("Trip duration distribution", [b[0] for b in report.duration_buckets],
                                    [b[1] for b in report.duration_buckets], width=half, subtitle="Number of trips",
                                    integer=True))
    if charts:
        story += [PageBreak(), pdf.section("3. Charts"), pdf.chart_grid(charts)]

    # -- vehicle table --
    vrows = []
    for r in report.vehicle_rows:
        vrows.append([
            r["registration_number"], r["driver"] or "Unassigned", r["trips"], fmt_km(r["distance_km"]),
            fmt_duration(r["travel_seconds"]), fmt_duration(r["moving_seconds"]), fmt_duration(r["idle_seconds"]),
            fmt_duration(r["engine_off_seconds"]), r["stops"], fmt_speed(r["avg_speed_kmh"]),
            fmt_speed(r["max_speed_kmh"]), fmt_dt(r["first_start"], tz, "%d %b %H:%M"),
            fmt_dt(r["last_end"], tz, "%d %b %H:%M"), fmt_pct(r["utilization_pct"]),
        ])
    vrows.append([
        "TOTAL / FLEET", "", s["total_trips"], fmt_km(s["total_distance_km"]), fmt_duration(s["travel_seconds"]),
        fmt_duration(s["moving_seconds"]), fmt_duration(s["idle_seconds"]), fmt_duration(s["engine_off_seconds"]),
        s["stops"], fmt_speed(s["avg_speed_kmh"]), fmt_speed(s["max_speed_kmh"]), "", "",
        fmt_pct(s["fleet_utilization_pct"]),
    ])
    story += [PageBreak(), pdf.section("4. Vehicle-wise Analysis", "Idle = engine on, not moving. Stopped = engine off "
                                       "inside a trip. Start/End = first trip start and last trip end in the period."),
              pdf.table(["Vehicle", "Driver", "Trips", "Distance", "Travel time", "Driving", "Idle", "Stopped",
                         "Stops", "Avg speed", "Max speed", "First start", "Last end", "Utilization"],
                        vrows, [1.1, 1.2, 0.5, 0.8, 0.8, 0.75, 0.7, 0.7, 0.5, 0.8, 0.8, 0.85, 0.85, 0.75],
                        align_right=(2, 3, 4, 5, 6, 7, 8, 9, 10, 13), bold_last=True, wrap=(1,))]

    # -- day table --
    def day_dur(d, key):
        return fmt_duration(d[key]) if d["trips"] else DASH

    drows = [[f"{d['date']:%a %d %b %Y}", d["trips"], d["vehicles"], fmt_km(d["distance_km"]),
              day_dur(d, "travel_seconds"), day_dur(d, "moving_seconds"), day_dur(d, "idle_seconds"),
              fmt_speed(d["avg_speed_kmh"]), fmt_speed(d["max_speed_kmh"])] for d in report.day_rows]
    drows.append(["TOTAL", s["total_trips"], s["vehicles_with_trips"], fmt_km(s["total_distance_km"]),
                  fmt_duration(s["travel_seconds"]), fmt_duration(s["moving_seconds"]), fmt_duration(s["idle_seconds"]),
                  fmt_speed(s["avg_speed_kmh"]), fmt_speed(s["max_speed_kmh"])])
    story += [pdf.section("5. Day-wise Analysis", "Trips are counted on the local date they started."),
              pdf.table(["Date", "Trips", "Vehicles", "Distance", "Travel time", "Driving", "Idle", "Avg speed",
                         "Max speed"], drows, [1.4, 0.6, 0.7, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
                        align_right=range(1, 9), bold_last=True)]

    # -- trips --
    trows = []
    for rec in sorted(report.trips, key=lambda r: r.trip.start_at):
        t, a = rec.trip, rec.analysis
        trows.append([
            rec.trip_id, t.vehicle.registration_number, rec.driver_name or "Unassigned",
            t.start_location or fmt_coord(t.start_latitude, t.start_longitude),
            t.end_location or fmt_coord(t.end_latitude, t.end_longitude),
            fmt_dt(t.start_at, tz, "%d %b %H:%M:%S"),
            fmt_dt(t.end_at, tz, "%d %b %H:%M:%S") if t.end_at else "Ongoing",
            fmt_duration(a.duration_seconds),
            fmt_km(a.distance_km) + (" GPS est." if a.distance_source == "GPS" else ""),
            fmt_speed(a.avg_speed_kmh), fmt_speed(a.max_speed_kmh), status_label(t.status),
        ])
    if trows:
        story += [PageBreak(), pdf.section("6. Trip Details", f"{len(trows)} trips, oldest first."),
                  pdf.table(["Trip ID", "Vehicle", "Driver", "Start location", "End location", "Start", "End",
                             "Duration", "Distance", "Avg speed", "Max speed", "Status"], trows,
                            [1.25, 0.8, 0.8, 1.7, 1.7, 0.8, 0.8, 0.6, 0.7, 0.7, 0.7, 0.65],
                            align_right=(7, 8, 9, 10), wrap=(0, 2, 3, 4))]
    else:
        story += [pdf.section("6. Trip Details"), pdf.p("No trips were detected in this period.")]

    # -- stops --
    if report.stops:
        longest = sorted(report.stops, key=lambda rs: rs[1].duration_seconds, reverse=True)[:50]
        srows = [[rec.trip_id, rec.vehicle.registration_number, fmt_dt(st.start_at, tz, "%d %b %H:%M:%S"),
                  fmt_dt(st.end_at, tz, "%H:%M:%S"), fmt_duration(st.duration_seconds),
                  "Engine off" if st.engine_off else "Idling",
                  st.location or fmt_coord(st.latitude, st.longitude)] for rec, st in longest]
        story += [pdf.section("7. Idle / Stoppage Analysis",
                              f"The {len(longest)} longest of {len(report.stops)} stops (≥ 1 min) inside trips."),
                  pdf.table(["Trip ID", "Vehicle", "From", "To", "Duration", "Type", "Location"], srows,
                            [1.3, 0.8, 0.9, 0.6, 0.6, 0.6, 3.2], align_right=(4,), wrap=(0, 6))]

    # No GPS History section: the fleet report is summary + analysis only. The
    # per-reading history is in each trip's own Trip Analysis Report.
    story += [Spacer(1, 12), pdf.section("Notes & Definitions"),
              pdf.p(_methodology(pdf, meta) + _FLEET_GPS_NOTE, "note")]
    return pdf.build(story)


def trip_report_pdf(report):
    from reportlab.platypus import Image, KeepTogether, PageBreak, Spacer

    meta, rec = report.meta, report.record
    t, a = rec.trip, rec.analysis
    tz = meta.tz
    pdf = _Pdf(meta)
    story = pdf.title_block()

    cards = [
        ("Duration", fmt_duration(a.duration_seconds), status_label(t.status)),
        ("Distance travelled", fmt_km(a.distance_km, 2), f"source: {a.distance_source}" if a.distance_source else ""),
        ("Average speed", fmt_speed(a.avg_speed_kmh), f"{fmt_speed(a.avg_moving_speed_kmh)} while moving"),
        ("Maximum speed", fmt_speed(a.max_speed_kmh),
         fmt_dt(a.max_speed_point.timestamp, tz, "at %H:%M:%S") if a.max_speed_point else ""),
        ("Moving time", fmt_duration(a.moving_seconds), ""),
        ("Idle time (engine on)", fmt_duration(a.idle_seconds), ""),
        ("Stopped (engine off)", fmt_duration(a.engine_off_seconds), f"{len(a.stops)} stops ≥ 1 min"),
        ("GPS records", f"{a.point_count:,}", f"{a.fix_count:,} with a satellite fix"),
    ]
    story += [pdf.section("Trip Summary"), pdf.kpi_grid(cards, cols=4)]
    first_fix, last_fix = a.first_fix, a.last_fix
    details = [
        ("Trip ID", rec.trip_id), ("Status", status_label(t.status)),
        ("Vehicle", f"{t.vehicle.registration_number}"
         + (f" ({t.vehicle.vehicle_type.name})" if t.vehicle.vehicle_type_id else "")),
        ("Driver", rec.driver_name or "Unassigned"),
        ("Start date", fmt_dt(t.start_at, tz, "%A, %d %b %Y")), ("Start time", fmt_dt(t.start_at, tz, "%H:%M:%S")),
        ("End date", fmt_dt(t.end_at, tz, "%A, %d %b %Y") if t.end_at else "Ongoing"),
        ("End time", fmt_dt(t.end_at, tz, "%H:%M:%S") if t.end_at else "Ongoing"),
        ("Start location", t.start_location or (first_fix.location if first_fix else "") or DASH),
        ("End location", t.end_location or (last_fix.location if last_fix else "") or DASH),
        ("Start coordinates", fmt_coord(first_fix.latitude, first_fix.longitude) if first_fix else DASH),
        ("End coordinates", fmt_coord(last_fix.latitude, last_fix.longitude) if last_fix else DASH),
        ("GPS path length", fmt_km(a.gps_distance_km, 2) if a.fix_count >= 2 else DASH),
        ("Longest stop", fmt_duration(a.longest_stop.duration_seconds) if a.longest_stop else "None"),
        ("Trip closure time", meta.closure_label),
    ]
    story += [Spacer(1, 4), pdf.key_value(details)]

    # -- map --
    png, basemap_ok = render_route_map(report.points, a.stops)
    if png:
        from reportlab.lib.utils import ImageReader

        img_w = pdf.content_width
        iw, ih = ImageReader(io.BytesIO(png)).getSize()
        img_h = img_w * ih / iw
        legend = ("A = start, B = end" + (", numbered orange = stops ranked by duration" if a.stops else "")
                  + f". Route drawn from {a.fix_count:,} recorded GPS fixes.")
        story += [PageBreak(), KeepTogether([pdf.section("Trip Route Map", legend),
                                             Image(io.BytesIO(png), width=img_w, height=img_h)])]
    else:
        story += [pdf.section("Trip Route Map"), pdf.p("No GPS fixes were recorded for this trip, so no route can be drawn.")]

    # -- charts --
    half = pdf.content_width / 2 - 12
    xs = [(ts - t.start_at).total_seconds() for ts, _s, _d in a.series]
    speed_ys = [float(sp) if sp is not None else None for _t, sp, _d in a.series]
    dist_ys = [d for _t, _s, d in a.series]
    charts = [
        pdf.line_chart("Speed vs time (km/h)", xs, speed_ys, width=half, start_at=t.start_at, tz=tz,
                       subtitle="Every recorded reading", reference=float(meta.moving_threshold_kmh)),
        pdf.line_chart("Distance vs time (km)", xs, dist_ys, width=half, start_at=t.start_at, tz=tz,
                       y_format="{:,.1f}", subtitle=f"Cumulative, from {a.distance_source.lower() or 'GPS'}"),
    ]
    states = [(ta.STATE_LABELS[k], a.state_seconds[k] / 60, STATE_COLORS[k]) for k in ta.STATE_LABELS
              if a.state_seconds[k] > 0]
    if states:
        charts.append(pdf.bar_chart("Movement vs idle (minutes)", [s[0] for s in states], [s[1] for s in states],
                                    width=half, horizontal=True, height=150, value_format="{:,.1f}",
                                    colors_per_bar=[s[2] for s in states], subtitle="Time between readings, by state"))
    if any(a.band_seconds):
        total_band = sum(a.band_seconds)
        charts.append(pdf.bar_chart("Speed profile (% of moving time)", [b[0] for b in a.bands],
                                    [v / total_band * 100 for v in a.band_seconds], width=half, height=150,
                                    value_format="{:,.1f}", colors_per_bar=SEQ_RAMP[2:2 + len(a.bands)] if len(a.bands) <= 6 else None))
    story += [PageBreak(), pdf.section("Trip Graphs"), pdf.chart_grid(charts)]

    story += [pdf.section("Trip Analysis"), pdf.insights_table(report.insights)]
    if a.stops:
        srows = [[i + 1, fmt_dt(st.start_at, tz, "%H:%M:%S"), fmt_dt(st.end_at, tz, "%H:%M:%S"),
                  fmt_duration(st.duration_seconds), "Engine off" if st.engine_off else "Idling",
                  fmt_coord(st.latitude, st.longitude), st.location or DASH]
                 for i, st in enumerate(sorted(a.stops, key=lambda s: s.duration_seconds, reverse=True))]
        story += [pdf.section("Stops", "Ranked by duration — the numbers match the markers on the map (top 15)."),
                  pdf.table(["#", "From", "To", "Duration", "Type", "Coordinates", "Location"], srows,
                            [0.3, 0.6, 0.6, 0.6, 0.6, 1.3, 4.0], align_right=(0, 3), wrap=(6,))]

    points = report.points
    indexed = list(enumerate(points))
    sample, sampled = _sample_rows(indexed, ta.PDF_MAX_GPS_ROWS_TRIP)
    note = f"All {len(points):,} GPS records of this trip, in order."
    if sampled:
        note = (f"{len(points):,} GPS records; {len(sample):,} evenly spaced records shown to keep the PDF printable "
                "(first and last included). The Excel export contains every record.")
    grows = [[i + 1, fmt_dt(p.timestamp, tz, "%d %b %Y %H:%M:%S"),
              f"{float(p.latitude):.6f}" if p.has_fix else "No fix",
              f"{float(p.longitude):.6f}" if p.has_fix else "No fix",
              f"{float(p.speed):.1f}" if p.speed is not None else DASH,
              {True: "On", False: "Off"}.get(p.ignition, DASH), _state_of(a, points, i),
              _short(p.location, 86)] for i, p in sample]
    story += [PageBreak(), pdf.section("Complete GPS History", note + f" Vehicle {t.vehicle.registration_number}, "
                                       f"driver {rec.driver_name or 'unassigned'}.")]
    story.append(pdf.table(["#", "Timestamp", "Latitude", "Longitude", "Speed km/h", "Ignition", "State (to next)",
                            "Location"], grows, [0.35, 1.1, 0.7, 0.7, 0.6, 0.5, 0.9, 3.6],
                           align_right=(0, 2, 3, 4), dense=True)
                 if grows else pdf.p("No GPS records were stored for this trip."))
    story += [Spacer(1, 12), pdf.section("Notes & Definitions"), pdf.p(_methodology(pdf, meta), "note")]
    return pdf.build(story)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

FMT_DT = "dd-mmm-yyyy hh:mm:ss"
FMT_DATE = "dd-mmm-yyyy"
FMT_TIME = "hh:mm:ss"
FMT_DUR = "[h]:mm:ss"
FMT_KM = "#,##0.00"
FMT_SPEED = "0.0"
FMT_INT = "#,##0"
FMT_PCT = "0.0%"
FMT_COORD = "0.000000"


def _xl_dt(value, tz):
    return value.astimezone(tz).replace(tzinfo=None) if value else None


def _xl_dur(seconds):
    return seconds / 86400 if seconds is not None else None


class _Xlsx:
    def __init__(self, meta):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        self.meta = meta
        self.wb = Workbook(write_only=True)
        self.wb.properties.title = f"{meta.title} — {meta.subtitle}"
        self.wb.properties.creator = meta.app_name
        self.Font, self.PatternFill, self.Alignment = Font, PatternFill, Alignment
        self.header_font = Font(bold=True, color="FFFFFF")
        self.header_fill = PatternFill("solid", fgColor=INK[1:])
        self.total_font = Font(bold=True)
        self.total_fill = PatternFill("solid", fgColor="E0E7FF")
        self.title_font = Font(bold=True, size=16, color=INK[1:])
        self.h2_font = Font(bold=True, size=12, color=INK[1:])
        self.muted_font = Font(italic=True, size=9, color=INK_2[1:])
        self.label_font = Font(color=INK_2[1:])
        self.bold = Font(bold=True)
        self.thin_bottom = Border(bottom=Side(style="thin", color=RULE[1:]))
        self.wrap = Alignment(wrap_text=True, vertical="top")

    def cell(self, ws, value, *, fmt=None, font=None, fill=None, align=None):
        from openpyxl.cell import WriteOnlyCell

        c = WriteOnlyCell(ws, value=value)
        if fmt:
            c.number_format = fmt
        if font:
            c.font = font
        if fill:
            c.fill = fill
        if align:
            c.alignment = align
        return c

    def data_sheet(self, title, columns, rows, *, totals=None, charts=(), widths=None):
        """columns: [(header, number_format | None, width_hint | None)].
        rows: lists of typed values. totals: {col_index: "sum"|"max"|"label:TEXT"|formula-callable}.
        Header on row 1 (filter + freeze), optional SUBTOTAL row after the data."""
        from openpyxl.utils import get_column_letter

        ws = self.wb.create_sheet(title)
        for j, (header, _fmt, hint) in enumerate(columns):
            width = hint
            if width is None:
                sample = [len(str(header))] + [len(_display(r[j])) for r in rows[:500]]
                width = min(max(sample) + 3, 60)
            ws.column_dimensions[get_column_letter(j + 1)].width = max(width, 9)
        ws.freeze_panes = "B2" if len(columns) > 6 else "A2"
        ws.append([self.cell(ws, h, font=self.header_font, fill=self.header_fill,
                             align=self.Alignment(vertical="center", wrap_text=True)) for h, _f, _w in columns])
        for row in rows:
            ws.append([self.cell(ws, v, fmt=columns[j][1]) if v is not None else None for j, v in enumerate(row)])
        last = len(rows) + 1
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last}"
        if totals and rows:
            out = []
            for j, (_h, fmt, _w) in enumerate(columns):
                spec = totals.get(j)
                col = get_column_letter(j + 1)
                if spec is None:
                    out.append(self.cell(ws, None, fill=self.total_fill))
                elif spec == "sum":
                    out.append(self.cell(ws, f"=SUBTOTAL(109,{col}2:{col}{last})", fmt=fmt, font=self.total_font,
                                         fill=self.total_fill))
                elif spec == "max":
                    out.append(self.cell(ws, f"=SUBTOTAL(104,{col}2:{col}{last})", fmt=fmt, font=self.total_font,
                                         fill=self.total_fill))
                elif callable(spec):
                    out.append(self.cell(ws, spec(last), fmt=fmt, font=self.total_font, fill=self.total_fill))
                else:
                    out.append(self.cell(ws, spec, font=self.total_font, fill=self.total_fill))
            ws.append(out)
        for chart, anchor in charts:
            ws.add_chart(chart, anchor)
        return ws

    def save(self):
        out = io.BytesIO()
        self.wb.save(out)
        return out.getvalue()


def _display(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, datetime.datetime):
        return "00-Mon-0000 00:00:00"
    if isinstance(value, datetime.date):
        return "00-Mon-0000"
    return str(value)


def _bar(title, y_title, data_ref, cats_ref, *, horizontal=False, color=PRIMARY, width=16, height=8, stacked=False,
         series_colors=None, legend=False):
    from openpyxl.chart import BarChart

    chart = BarChart()
    chart.type = "bar" if horizontal else "col"
    chart.title = title
    chart.y_axis.title = y_title
    chart.y_axis.majorGridlines = chart.y_axis.majorGridlines
    chart.width, chart.height = width, height
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    chart.gapWidth = 60
    if stacked:
        chart.grouping = "stacked"
        chart.overlap = 100
    for i, s in enumerate(chart.series):
        s.graphicalProperties.solidFill = (series_colors[i] if series_colors else color)[1:]
        s.graphicalProperties.line.solidFill = "FFFFFF"
    if not legend:
        chart.legend = None
    else:
        chart.legend.position = "b"
    # openpyxl charts default to hidden axes in some Excel builds — force visible.
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    return chart


def _line(title, y_title, data_ref, cats_ref, *, width=24, height=9, color=PRIMARY):
    from openpyxl.chart import LineChart

    chart = LineChart()
    chart.title = title
    chart.y_axis.title = y_title
    chart.width, chart.height = width, height
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    s = chart.series[0]
    s.graphicalProperties.line.solidFill = color[1:]
    s.graphicalProperties.line.width = 15000
    s.smooth = False
    chart.legend = None
    chart.x_axis.number_format = "hh:mm"
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.x_axis.tickLblSkip = None
    return chart


def _summary_sheet(x, title, metrics, insights, notes, *, image_png=None):
    """Title block + Metric/Value table + findings. metrics: [(label, value, fmt)]."""
    ws = x.wb.create_sheet(title)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 90
    m = x.meta
    ws.append([x.cell(ws, m.title, font=x.title_font)])
    ws.append([x.cell(ws, m.subtitle, font=x.h2_font)])
    for label, value in (("Company", m.company), ("Period", m.period_label), ("Scope", m.filters_label),
                         ("Timezone", m.tz_name), ("Generated", m.generated_at.strftime("%d %b %Y %H:%M:%S"))):
        ws.append([x.cell(ws, label, font=x.label_font), x.cell(ws, value)])
    ws.append([])
    ws.append([x.cell(ws, h, font=x.header_font, fill=x.header_fill) for h in ("Metric", "Value", "")])
    for label, value, fmt in metrics:
        ws.append([x.cell(ws, label, font=x.label_font), x.cell(ws, value, fmt=fmt, font=x.bold)])
    ws.append([])
    if insights:
        ws.append([x.cell(ws, h, font=x.header_font, fill=x.header_fill) for h in ("Finding", "Result", "Detail")])
        for label, value, detail in insights:
            ws.append([x.cell(ws, label, font=x.bold), x.cell(ws, value), x.cell(ws, detail, align=x.wrap)])
        ws.append([])
    for note in notes:
        ws.append([x.cell(ws, note, font=x.muted_font)])
    if image_png:
        from openpyxl.drawing.image import Image as XlImage

        img = XlImage(io.BytesIO(image_png))
        scale = 760 / img.width
        img.width, img.height = int(img.width * scale), int(img.height * scale)
        ws.add_image(img, "E2")
    return ws


def period_report_xlsx(report):
    from openpyxl.chart import Reference

    meta, s = report.meta, report.summary
    tz = meta.tz
    x = _Xlsx(meta)

    notes = [_methodology(None, meta) + _FLEET_GPS_NOTE]
    if s["gps_distance_trips"]:
        notes.insert(0, f"{s['gps_distance_trips']} trip(s) had no odometer reading: their GPS path estimate "
                        f"({fmt_km(s['gps_estimated_km'])}) is in Trip Details (source “GPS”) but not in distance "
                        "totals, which match the Trip Report page.")
    metrics = [
        ("Total vehicles (tracked)", s["total_vehicles"], FMT_INT),
        ("Vehicles with trips", s["vehicles_with_trips"], FMT_INT),
        ("Total trips", s["total_trips"], FMT_INT),
        ("Completed trips", s["completed_trips"], FMT_INT),
        ("In-progress (incomplete) trips", s["active_trips"], FMT_INT),
        ("Total distance (km, odometer)", s["total_distance_km"], FMT_KM),
        ("GPS-estimated distance not in total (km)", s["gps_estimated_km"], FMT_KM),
        ("Total travel time", _xl_dur(s["travel_seconds"]), FMT_DUR),
        ("Total driving (moving) time", _xl_dur(s["moving_seconds"]), FMT_DUR),
        ("Total idle time (engine on)", _xl_dur(s["idle_seconds"]), FMT_DUR),
        ("Total stopped time in trips (engine off)", _xl_dur(s["engine_off_seconds"]), FMT_DUR),
        ("Time without data inside trips", _xl_dur(s["no_data_seconds"]), FMT_DUR),
        ("Average trip duration", _xl_dur(s["avg_trip_seconds"]), FMT_DUR),
        ("Average trip distance (km)", s["avg_trip_km"], FMT_KM),
        ("Average speed (km/h)", s["avg_speed_kmh"], FMT_SPEED),
        ("Average moving speed (km/h)", s["avg_moving_speed_kmh"], FMT_SPEED),
        ("Maximum speed (km/h)", s["max_speed_kmh"], FMT_SPEED),
        ("Stops ≥ 1 min", s["stops"], FMT_INT),
        ("Fleet utilization", s["fleet_utilization_pct"] / 100 if s["fleet_utilization_pct"] is not None else None,
         FMT_PCT),
        ("GPS records in period", s["gps_points"], FMT_INT),
    ]
    _summary_sheet(x, "Summary", metrics, report.insights, notes)

    # Vehicle Analysis
    vcols = [("Vehicle", None, None), ("Type", None, None), ("Driver", None, None), ("Trips", FMT_INT, 8),
             ("Completed", FMT_INT, 11), ("Distance (km)", FMT_KM, 13), ("Travel time", FMT_DUR, 12),
             ("Driving time", FMT_DUR, 12), ("Idle time", FMT_DUR, 11), ("Stopped (engine off)", FMT_DUR, 13),
             ("Stops ≥1 min", FMT_INT, 10), ("Avg speed (km/h)", FMT_SPEED, 11), ("Max speed (km/h)", FMT_SPEED, 11),
             ("First trip start", FMT_DT, 20), ("Last trip end", FMT_DT, 20), ("Longest trip", FMT_DUR, 12),
             ("Utilization", FMT_PCT, 11), ("GPS records in trips", FMT_INT, 12),
             ("Trip closure (min)", FMT_INT, 11)]
    vrows = [[r["registration_number"], r["vehicle_type"], r["driver"] or "Unassigned", r["trips"], r["completed"],
              r["distance_km"], _xl_dur(r["travel_seconds"]), _xl_dur(r["moving_seconds"]), _xl_dur(r["idle_seconds"]),
              _xl_dur(r["engine_off_seconds"]), r["stops"], r["avg_speed_kmh"], r["max_speed_kmh"],
              _xl_dt(r["first_start"], tz), _xl_dt(r["last_end"], tz), _xl_dur(r["longest_trip_seconds"]),
              r["utilization_pct"] / 100, r["gps_points"], r["vehicle"].trip_closure_minutes] for r in report.vehicle_rows]
    n = len(vrows)
    vtotals = {0: "TOTAL (visible rows)", 3: "sum", 4: "sum", 5: "sum", 6: "sum", 7: "sum", 8: "sum", 9: "sum",
               10: "sum", 12: "max", 17: "sum",
               11: lambda last: f'=IFERROR(F{last + 1}/(G{last + 1}*24),"")'}
    ws_v = x.data_sheet("Vehicle Analysis", vcols, vrows, totals=vtotals)
    if n:
        ws_v.add_chart(_bar("Distance by vehicle (km)", "km", Reference(ws_v, min_col=6, min_row=1, max_row=n + 1),
                            Reference(ws_v, min_col=1, min_row=2, max_row=n + 1), horizontal=True,
                            height=max(7, 1 + 0.6 * n)), "T2")
        ws_v.add_chart(_bar("Trip time by vehicle", "h:mm", Reference(ws_v, min_col=8, max_col=10, min_row=1, max_row=n + 1),
                            Reference(ws_v, min_col=1, min_row=2, max_row=n + 1), horizontal=True, stacked=True,
                            series_colors=[STATE_COLORS["moving"], STATE_COLORS["idle"], STATE_COLORS["engine_off"]],
                            legend=True, height=max(7, 1 + 0.6 * n)), f"T{int(max(16, 3 + 1.3 * n))}")

    # Daily Analysis
    dcols = [("Date", FMT_DATE, 14), ("Trips", FMT_INT, 8), ("Vehicles", FMT_INT, 10), ("Distance (km)", FMT_KM, 13),
             ("Travel time", FMT_DUR, 12), ("Driving time", FMT_DUR, 12), ("Idle time", FMT_DUR, 11),
             ("Avg speed (km/h)", FMT_SPEED, 11), ("Max speed (km/h)", FMT_SPEED, 11)]
    drows = [[d["date"], d["trips"], d["vehicles"], d["distance_km"], _xl_dur(d["travel_seconds"]),
              _xl_dur(d["moving_seconds"]), _xl_dur(d["idle_seconds"]), d["avg_speed_kmh"], d["max_speed_kmh"]]
             for d in report.day_rows]
    dn = len(drows)
    ws_d = x.data_sheet("Daily Analysis", dcols, drows, totals={
        0: "TOTAL", 1: "sum", 3: "sum", 4: "sum", 5: "sum", 6: "sum", 8: "max",
        7: lambda last: f'=IFERROR(D{last + 1}/(E{last + 1}*24),"")'})
    if dn:
        cats = Reference(ws_d, min_col=1, min_row=2, max_row=dn + 1)
        c1 = _bar("Trips per day", "Trips", Reference(ws_d, min_col=2, min_row=1, max_row=dn + 1), cats)
        c2 = _bar("Distance per day (km)", "km", Reference(ws_d, min_col=4, min_row=1, max_row=dn + 1), cats)
        c1.x_axis.number_format = c2.x_axis.number_format = "dd-mmm"
        ws_d.add_chart(c1, "K2")
        ws_d.add_chart(c2, "K19")

    # Trip Details
    tcols = [("Trip ID", None, 26), ("Vehicle", None, None), ("Driver", None, None), ("Status", None, 11),
             ("Start time", FMT_DT, 20), ("End time", FMT_DT, 20), ("Duration", FMT_DUR, 11),
             ("Distance (km, odometer)", FMT_KM, 13), ("GPS path (km)", FMT_KM, 11), ("Avg speed (km/h)", FMT_SPEED, 11),
             ("Avg moving speed (km/h)", FMT_SPEED, 12), ("Max speed (km/h)", FMT_SPEED, 11),
             ("Driving time", FMT_DUR, 11), ("Idle time", FMT_DUR, 11), ("Stopped (engine off)", FMT_DUR, 12),
             ("Stops ≥1 min", FMT_INT, 9), ("GPS records", FMT_INT, 10), ("Start latitude", FMT_COORD, 12),
             ("Start longitude", FMT_COORD, 12), ("Start location", None, 50), ("End latitude", FMT_COORD, 12),
             ("End longitude", FMT_COORD, 12), ("End location", None, 50)]
    trows = []
    for rec in sorted(report.trips, key=lambda r: r.trip.start_at):
        t, a = rec.trip, rec.analysis
        trows.append([
            rec.trip_id, t.vehicle.registration_number, rec.driver_name or "Unassigned", status_label(t.status),
            _xl_dt(t.start_at, tz), _xl_dt(t.end_at, tz), _xl_dur(a.duration_seconds), a.odometer_km,
            a.gps_distance_km if a.fix_count >= 2 else None, a.avg_speed_kmh, a.avg_moving_speed_kmh, a.max_speed_kmh,
            _xl_dur(a.moving_seconds), _xl_dur(a.idle_seconds), _xl_dur(a.engine_off_seconds), len(a.stops),
            a.point_count, _f(t.start_latitude), _f(t.start_longitude), t.start_location or None,
            _f(t.end_latitude), _f(t.end_longitude), t.end_location or None,
        ])
    x.data_sheet("Trip Details", tcols, trows, totals={0: "TOTAL (visible rows)", 6: "sum", 7: "sum", 8: "sum", 11: "max",
                                                       12: "sum", 13: "sum", 14: "sum", 15: "sum", 16: "sum"})

    # Stops
    scols = [("Trip ID", None, 26), ("Vehicle", None, None), ("Start", FMT_DT, 20), ("End", FMT_DT, 20),
             ("Duration", FMT_DUR, 11), ("Type", None, 11), ("Latitude", FMT_COORD, 12), ("Longitude", FMT_COORD, 12),
             ("Location", None, 60)]
    srows = [[rec.trip_id, rec.vehicle.registration_number, _xl_dt(st.start_at, tz), _xl_dt(st.end_at, tz),
              _xl_dur(st.duration_seconds), "Engine off" if st.engine_off else "Idling", _f(st.latitude),
              _f(st.longitude), st.location or None] for rec, st in report.stops]
    x.data_sheet("Stops", scols, srows, totals={0: "TOTAL (visible rows)", 4: "sum"})

    # Analysis (distributions + charts)
    ws_a = x.wb.create_sheet("Analysis")
    for col, width in zip("ABCDEFGH", (22, 14, 14, 4, 12, 10, 4, 14)):
        ws_a.column_dimensions[col].width = width
    total_band = sum(report.band_seconds) or 0
    ws_a.append([x.cell(ws_a, h, font=x.header_font, fill=x.header_fill) for h in ("Speed band", "Moving time", "Share")]
                + [None, x.cell(ws_a, "Hour", font=x.header_font, fill=x.header_fill),
                   x.cell(ws_a, "Trip starts", font=x.header_font, fill=x.header_fill), None,
                   x.cell(ws_a, "Trip duration", font=x.header_font, fill=x.header_fill),
                   x.cell(ws_a, "Trips", font=x.header_font, fill=x.header_fill)])
    for i in range(24):
        row = [None, None, None]
        if i < len(report.band_labels):
            secs = report.band_seconds[i]
            row = [report.band_labels[i], x.cell(ws_a, _xl_dur(secs), fmt=FMT_DUR),
                   x.cell(ws_a, secs / total_band if total_band else None, fmt=FMT_PCT)]
        row += [None, f"{i:02d}:00", report.hour_counts[i], None]
        if i < len(report.duration_buckets):
            row += list(report.duration_buckets[i])
        ws_a.append(row)
    nb = len(report.band_labels)
    ws_a.add_chart(_bar("Speed profile — share of moving time", "Share", Reference(ws_a, min_col=3, min_row=1, max_row=nb + 1),
                        Reference(ws_a, min_col=1, min_row=2, max_row=nb + 1)), "K2")
    ws_a.add_chart(_bar("Trip starts by hour of day", "Trips", Reference(ws_a, min_col=6, min_row=1, max_row=25),
                        Reference(ws_a, min_col=5, min_row=2, max_row=25)), "K19")
    ws_a.add_chart(_bar("Trip duration distribution", "Trips",
                        Reference(ws_a, min_col=9, min_row=1, max_row=len(report.duration_buckets) + 1),
                        Reference(ws_a, min_col=8, min_row=2, max_row=len(report.duration_buckets) + 1)), "K36")
    # No GPS History sheet — see period_report_pdf.
    return x.save()


def _f(value):
    return float(value) if value is not None else None


def trip_report_xlsx(report):
    from openpyxl.chart import Reference

    meta, rec = report.meta, report.record
    t, a = rec.trip, rec.analysis
    tz = meta.tz
    x = _Xlsx(meta)
    first_fix, last_fix = a.first_fix, a.last_fix

    metrics = [
        ("Trip ID", rec.trip_id, None),
        ("Vehicle", t.vehicle.registration_number, None),
        ("Vehicle type", t.vehicle.vehicle_type.name if t.vehicle.vehicle_type_id else "", None),
        ("Driver", rec.driver_name or "Unassigned", None),
        ("Status", status_label(t.status), None),
        ("Start date", _xl_dt(t.start_at, tz).date(), FMT_DATE),
        ("Start time", _xl_dt(t.start_at, tz).time(), FMT_TIME),
        ("End date", _xl_dt(t.end_at, tz).date() if t.end_at else "Ongoing", FMT_DATE),
        ("End time", _xl_dt(t.end_at, tz).time() if t.end_at else "Ongoing", FMT_TIME),
        ("Start location", t.start_location or (first_fix.location if first_fix else ""), None),
        ("End location", t.end_location or (last_fix.location if last_fix else ""), None),
        ("Start coordinates", fmt_coord(first_fix.latitude, first_fix.longitude) if first_fix else "", None),
        ("End coordinates", fmt_coord(last_fix.latitude, last_fix.longitude) if last_fix else "", None),
        ("Trip duration", _xl_dur(a.duration_seconds), FMT_DUR),
        ("Distance travelled (km)", a.distance_km, FMT_KM),
        ("Distance source", a.distance_source, None),
        ("GPS path length (km)", a.gps_distance_km if a.fix_count >= 2 else None, FMT_KM),
        ("Moving time", _xl_dur(a.moving_seconds), FMT_DUR),
        ("Idle time (engine on)", _xl_dur(a.idle_seconds), FMT_DUR),
        ("Stopped time (engine off)", _xl_dur(a.engine_off_seconds), FMT_DUR),
        ("Time without data", _xl_dur(a.no_data_seconds), FMT_DUR),
        ("Average speed (km/h)", a.avg_speed_kmh, FMT_SPEED),
        ("Average moving speed (km/h)", a.avg_moving_speed_kmh, FMT_SPEED),
        ("Maximum speed (km/h)", a.max_speed_kmh, FMT_SPEED),
        ("Number of stops (≥ 1 min)", len(a.stops), FMT_INT),
        ("Longest stop", _xl_dur(a.longest_stop.duration_seconds) if a.longest_stop else None, FMT_DUR),
        ("GPS records", a.point_count, FMT_INT),
        ("GPS records with a fix", a.fix_count, FMT_INT),
        ("Trip closure time (minutes)", t.vehicle.trip_closure_minutes, FMT_INT),
    ]
    png, _ok = render_route_map(report.points, a.stops, width=1200, height=700)
    _summary_sheet(x, "Trip Summary", metrics, report.insights, [_methodology(None, meta)], image_png=png)

    # GPS History first (charts on Trip Analysis reference it).
    points = report.points
    gcols = [("#", FMT_INT, 7), ("GPS timestamp", FMT_DT, 20), ("Date", FMT_DATE, 12), ("Time", FMT_TIME, 10),
             ("Latitude", FMT_COORD, 12), ("Longitude", FMT_COORD, 12), ("GPS fix", None, 8),
             ("Speed (km/h)", FMT_SPEED, 10), ("Ignition", None, 9), ("State (to next reading)", None, 20),
             ("Cumulative distance (km)", FMT_KM, 14), ("Odometer (km)", "0.0", 12), ("Heading (°)", FMT_INT, 9),
             ("Satellites", FMT_INT, 9), ("Vehicle", None, 13), ("Driver", None, 18), ("Location / address", None, 70)]
    grows = []
    capped = points[:ta.EXCEL_MAX_GPS_ROWS]
    for i, p in enumerate(capped):
        local = _xl_dt(p.timestamp, tz)
        cum = a.series[i][2] if i < len(a.series) else None
        grows.append([i + 1, local, local.date(), local.time(), _f(p.latitude) if p.has_fix else None,
                      _f(p.longitude) if p.has_fix else None, "Yes" if p.has_fix else "No", _f(p.speed),
                      {True: "On", False: "Off"}.get(p.ignition), _state_of(a, points, i), cum, _f(p.odometer),
                      p.heading, p.satellites, t.vehicle.registration_number, rec.driver_name or None,
                      p.location or None])

    # Trip Analysis
    ws_a = x.wb.create_sheet("Trip Analysis")
    for col, width in zip("ABCDEFGHI", (24, 14, 10, 4, 16, 14, 10, 4, 4)):
        ws_a.column_dimensions[col].width = width
    hdr = dict(font=x.header_font, fill=x.header_fill)
    ws_a.append([x.cell(ws_a, "State", **hdr), x.cell(ws_a, "Time", **hdr), x.cell(ws_a, "Share", **hdr), None,
                 x.cell(ws_a, "Speed band", **hdr), x.cell(ws_a, "Moving time", **hdr), x.cell(ws_a, "Share", **hdr)])
    total_state = sum(a.state_seconds.values()) or 0
    total_band = sum(a.band_seconds) or 0
    state_keys = list(ta.STATE_LABELS)
    for i in range(max(len(state_keys), len(a.bands))):
        row = [None, None, None, None]
        if i < len(state_keys):
            secs = a.state_seconds[state_keys[i]]
            row = [ta.STATE_LABELS[state_keys[i]], x.cell(ws_a, _xl_dur(secs), fmt=FMT_DUR),
                   x.cell(ws_a, secs / total_state if total_state else None, fmt=FMT_PCT), None]
        if i < len(a.bands):
            secs = a.band_seconds[i]
            row += [a.bands[i][0], x.cell(ws_a, _xl_dur(secs), fmt=FMT_DUR),
                    x.cell(ws_a, secs / total_band if total_band else None, fmt=FMT_PCT)]
        ws_a.append(row)
    ws_a.append([])
    ws_a.append([x.cell(ws_a, "Stops (≥ 1 min), longest first", font=x.h2_font)])
    ws_a.append([x.cell(ws_a, h, **hdr) for h in ("From", "To", "Duration", "Type", "Latitude", "Longitude", "Location")])
    for st in sorted(a.stops, key=lambda s: s.duration_seconds, reverse=True):
        ws_a.append([x.cell(ws_a, _xl_dt(st.start_at, tz), fmt=FMT_DT), x.cell(ws_a, _xl_dt(st.end_at, tz), fmt=FMT_DT),
                     x.cell(ws_a, _xl_dur(st.duration_seconds), fmt=FMT_DUR),
                     "Engine off" if st.engine_off else "Idling", x.cell(ws_a, _f(st.latitude), fmt=FMT_COORD),
                     x.cell(ws_a, _f(st.longitude), fmt=FMT_COORD), st.location or None])
    ns = len(state_keys)
    ws_a.add_chart(_bar("Movement vs idle", "Share of time", Reference(ws_a, min_col=3, min_row=1, max_row=ns + 1),
                        Reference(ws_a, min_col=1, min_row=2, max_row=ns + 1), horizontal=True, width=14, height=7), "K2")
    ws_a.add_chart(_bar("Speed profile — share of moving time", "Share",
                        Reference(ws_a, min_col=7, min_row=1, max_row=len(a.bands) + 1),
                        Reference(ws_a, min_col=5, min_row=2, max_row=len(a.bands) + 1), width=14, height=7), "T2")
    # The GPS sheet is written after Trip Analysis (sheet order), but the line
    # charts on Trip Analysis plot its columns — chart references are resolved
    # by sheet title when the workbook is saved.
    ws_g = x.data_sheet("GPS History", gcols, grows)
    ng = len(grows)
    if ng >= 2:
        cats = Reference(ws_g, min_col=2, min_row=2, max_row=ng + 1)
        ws_a.add_chart(_line("Speed vs time (km/h)", "km/h", Reference(ws_g, min_col=8, min_row=1, max_row=ng + 1), cats),
                       "K17")
        ws_a.add_chart(_line("Distance vs time (km)", "km", Reference(ws_g, min_col=11, min_row=1, max_row=ng + 1), cats),
                       "K36")
    return x.save()
