"""PDF / Excel for the Odometer Report — the same rows the page shows
(apps.tracking.odometer_report), rendered with the Trip Report's own building
blocks (apps.tracking.trip_exports: header/footer, KPI cards, tables, charts,
typed Excel sheets) so both reports look and behave the same.

Device Odometer and GPS Odometer stay in separate, clearly labelled columns
everywhere; a difference is only shown where both exist.
"""

from apps.tracking import odometer_report as orp
from apps.tracking import trip_analytics as ta
from apps.tracking.trip_exports import (
    DASH,
    FMT_DATE,
    FMT_INT,
    PRIMARY,
    _bar,
    _Pdf,
    _summary_sheet,
    _Xlsx,
    fmt_km,
)

DEVICE_COLOR = PRIMARY  # categorical slot 1
GPS_COLOR = "#eb6834"  # categorical slot 2
FMT_ODO = "#,##0.0"
FMT_GPS = "#,##0.000"
FMT_PCT1 = '0.0"%"'


def _meta(report, *, range_key, vehicle_uuid, search, now):
    label = ta.RANGE_LABELS.get(range_key, "Custom Range")
    s, e = report.start_date, report.end_date
    span = f"{s:%d %b %Y}" if s == e else f"{s:%d %b %Y} – {e:%d %b %Y}"
    filters = []
    if vehicle_uuid and report.vehicles:
        filters.append(f"Vehicle: {report.vehicles[0].registration_number}")
    if search.strip():
        filters.append(f"Search: “{search.strip()}”")
    return ta._meta(
        title="Odometer Report", subtitle=f"{label} · {span}", period_start=report.period_start,
        period_end=report.period_end, period_label=f"{label} ({span})",
        filters_label=", ".join(filters) or "All tracked vehicles", now=now, tz=report.tz, vehicles=report.vehicles,
    )


DEFINITIONS = (
    "Device Odometer = the vehicle/device's own odometer counter as stored in the GPS history. Start is the last reading "
    "before the day (the value at midnight, so travel across midnight is not lost), else the day's first reading; End is "
    "the day's last reading; Distance = End − Start. If the counter was reset or misbehaved, Distance is the sum of the "
    "day's valid increments and the row says so. "
    "GPS Odometer = a counter computed only from latitude/longitude: the cumulative great-circle distance between "
    f"consecutive valid GPS fixes since the vehicle's first recorded fix (no-fix points skipped; hops faster than "
    f"{ta.MAX_PLAUSIBLE_SPEED_KMH} km/h ignored). Distance = End − Start for the day. "
    "The two are independent measurements and are never mixed. Difference = Device − GPS, shown only where both exist. "
    "N/A = not enough data to calculate (nothing is estimated)."
)


def _km(value, places=1):
    return "N/A" if value is None else f"{value:,.{places}f}"


def _pct(value):
    return "N/A" if value is None else f"{value:+.1f}%"


def _notes_text(row):
    return "; ".join(n["text"] for n in row.notes)


def _daily_totals(report):
    """Fleet totals per day (oldest first) — both sources, like-for-like."""
    days = sorted({r.date for r in report.rows})
    out = []
    for day in days:
        rows = [r for r in report.rows if r.date == day]
        device = orp._sum(r.device_distance for r in rows)
        gps = orp._sum(r.gps_distance for r in rows)
        out.append((day, device, gps))
    return out


def odometer_report_pdf(report, *, range_key, vehicle_uuid="", search="", now=None):
    from reportlab.platypus import PageBreak, Spacer

    meta = _meta(report, range_key=range_key, vehicle_uuid=vehicle_uuid, search=search, now=now)
    pdf = _Pdf(meta)
    s = report.summary
    story = pdf.title_block()

    cards = [
        ("Vehicles", f"{s['vehicles']}", f"{s['days_with_data']} of {s['vehicle_days']} vehicle-days with data"),
        ("Device Odometer distance", fmt_km(s["device_distance_km"]), "from the vehicle's own odometer"),
        ("GPS Odometer distance", fmt_km(s["gps_distance_km"], 1), "from latitude/longitude history"),
        ("Difference (Device − GPS)",
         "N/A" if s["difference_km"] is None else f"{s['difference_km']:+,.1f} km",
         "" if s["difference_pct"] is None else f"{s['difference_pct']:+.1f}% where both exist"),
        ("Vehicle-days with data issues", f"{s['days_with_issues']}", "see Notes column"),
    ]
    story += [pdf.section("1. Summary"), pdf.kpi_grid(cards, cols=5)]

    totals = _daily_totals(report)
    if any(d is not None or g is not None for _day, d, g in totals):
        chart = pdf.grouped_bar_chart(
            "Distance per day: Device vs GPS (km)", [f"{day:%d %b}" for day, *_ in totals],
            [("Device Odometer", [d for _day, d, _g in totals], DEVICE_COLOR),
             ("GPS Odometer", [g for _day, _d, g in totals], GPS_COLOR)],
            width=pdf.content_width, height=210, subtitle="All selected vehicles, per local day",
        )
        story += [Spacer(1, 6), chart]

    vrows = [[t["vehicle"].registration_number, t["days_with_data"], _km(t["device_distance_km"]),
              _km(t["gps_distance_km"], 1), "N/A" if t["difference_km"] is None else f"{t['difference_km']:+,.1f}",
              _pct(t["difference_pct"]), t["days_with_issues"]] for t in report.vehicle_totals]
    story += [pdf.section("2. Vehicle Summary"),
              pdf.table(["Vehicle", "Days with data", "Device distance (km)", "GPS distance (km)",
                         "Difference (km)", "Difference %", "Days with issues"],
                        vrows, [1.4, 0.8, 1.1, 1.1, 1.0, 0.9, 0.9], align_right=range(1, 7))]

    drows = [[f"{r.date:%d %b %Y}", r.vehicle.registration_number,
              _km(r.device_start), _km(r.device_end), _km(r.device_distance),
              _km(r.gps_start, 1), _km(r.gps_end, 1), _km(r.gps_distance, 1),
              "N/A" if r.difference_km is None else f"{r.difference_km:+,.1f}",
              _notes_text(r) or DASH] for r in report.rows]
    story += [PageBreak(), pdf.section("3. Daily Odometer", "Device Odometer and GPS Odometer side by side, per vehicle "
                                       "per day (km). N/A = not enough data."),
              pdf.table(["Date", "Vehicle", "Device start", "Device end", "Device distance", "GPS start", "GPS end",
                         "GPS distance", "Difference", "Notes"], drows,
                        [0.85, 0.9, 0.75, 0.75, 0.75, 0.75, 0.75, 0.75, 0.7, 2.6],
                        align_right=range(2, 9), wrap=(9,))]
    story += [Spacer(1, 12), pdf.section("Notes & Definitions"), pdf.p(DEFINITIONS, "note")]
    return pdf.build(story)


def odometer_report_xlsx(report, *, range_key, vehicle_uuid="", search="", now=None):
    from openpyxl.chart import Reference

    meta = _meta(report, range_key=range_key, vehicle_uuid=vehicle_uuid, search=search, now=now)
    x = _Xlsx(meta)
    s = report.summary
    metrics = [
        ("Vehicles", s["vehicles"], FMT_INT),
        ("Vehicle-days in period", s["vehicle_days"], FMT_INT),
        ("Vehicle-days with data", s["days_with_data"], FMT_INT),
        ("Vehicle-days with data issues", s["days_with_issues"], FMT_INT),
        ("Device Odometer distance (km)", s["device_distance_km"], FMT_ODO),
        ("GPS Odometer distance (km)", s["gps_distance_km"], FMT_GPS),
        ("Difference, Device − GPS (km, where both exist)", s["difference_km"], FMT_GPS),
        ("Difference % (where both exist)", s["difference_pct"], FMT_PCT1),
    ]
    _summary_sheet(x, "Summary", metrics, [], [DEFINITIONS])

    cols = [("Date", FMT_DATE, 13), ("Vehicle", None, None), ("Driver", None, None), ("Readings", FMT_INT, 10),
            ("Device Odometer Start (km)", FMT_ODO, 14), ("Device Odometer End (km)", FMT_ODO, 14),
            ("Device Odometer Distance (km)", FMT_ODO, 15), ("Device Start from", None, 16),
            ("GPS Odometer Start (km)", FMT_GPS, 14), ("GPS Odometer End (km)", FMT_GPS, 14),
            ("GPS Odometer Distance (km)", FMT_GPS, 15), ("Difference Device − GPS (km)", FMT_GPS, 15),
            ("Difference %", FMT_PCT1, 11), ("Status", None, 12), ("Notes", None, 70)]
    status_text = {orp.STATUS_OK: "OK", orp.STATUS_ISSUE: "Data issue", orp.STATUS_NO_DATA: "No data"}
    rows = [[r.date, r.vehicle.registration_number, r.driver_name or None, r.readings,
             r.device_start, r.device_end, r.device_distance,
             ("Previous day's last reading" if r.device_start_carried else "Day's first reading")
             if r.device_start is not None else None,
             r.gps_start, r.gps_end, r.gps_distance,
             orp._round(r.difference_km, 3), orp._round(r.difference_pct, 1),
             status_text[r.status], _notes_text(r) or None] for r in report.rows]
    x.data_sheet("Daily Odometer", cols, rows, totals={0: "TOTAL (visible rows)", 3: "sum", 6: "sum", 10: "sum", 11: "sum"})

    vcols = [("Vehicle", None, None), ("Days with data", FMT_INT, 12), ("Device Odometer Distance (km)", FMT_ODO, 16),
             ("GPS Odometer Distance (km)", FMT_GPS, 16), ("Difference Device − GPS (km)", FMT_GPS, 16),
             ("Difference %", FMT_PCT1, 12), ("Days with issues", FMT_INT, 12)]
    vrows = [[t["vehicle"].registration_number, t["days_with_data"], t["device_distance_km"], t["gps_distance_km"],
              t["difference_km"], t["difference_pct"], t["days_with_issues"]] for t in report.vehicle_totals]
    ws_v = x.data_sheet("Vehicle Summary", vcols, vrows, totals={0: "TOTAL (visible rows)", 1: "sum", 2: "sum",
                                                                  3: "sum", 4: "sum", 6: "sum"})
    n = len(vrows)
    if n:
        ws_v.add_chart(_bar("Distance by vehicle: Device vs GPS (km)", "km",
                            Reference(ws_v, min_col=3, max_col=4, min_row=1, max_row=n + 1),
                            Reference(ws_v, min_col=1, min_row=2, max_row=n + 1), horizontal=True,
                            series_colors=[DEVICE_COLOR, GPS_COLOR], legend=True, height=max(7, 2 + 0.8 * n)), "I2")

    totals = _daily_totals(report)
    dcols = [("Date", FMT_DATE, 13), ("Device Odometer Distance (km)", FMT_ODO, 16),
             ("GPS Odometer Distance (km)", FMT_GPS, 16)]
    ws_d = x.data_sheet("Daily Totals", dcols, [[day, d, g] for day, d, g in totals],
                        totals={0: "TOTAL", 1: "sum", 2: "sum"})
    if totals:
        chart = _bar("Distance per day: Device vs GPS (km)", "km",
                     Reference(ws_d, min_col=2, max_col=3, min_row=1, max_row=len(totals) + 1),
                     Reference(ws_d, min_col=1, min_row=2, max_row=len(totals) + 1),
                     series_colors=[DEVICE_COLOR, GPS_COLOR], legend=True, width=20)
        chart.x_axis.number_format = "dd-mmm"
        ws_d.add_chart(chart, "E2")
    return x.save()

