/*
 * FmsFormat — tiny, dependency-free display formatters shared by every
 * "live" page (Live Tracking, Trip Report, …) so a timestamp/duration reads
 * identically everywhere instead of each page re-implementing its own.
 */
(function (global) {
  "use strict";

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function timeAgo(isoTimestamp) {
    if (!isoTimestamp) return "—";
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(isoTimestamp).getTime()) / 1000));
    if (seconds < 60) return `${seconds} sec ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  // Renders an ISO timestamp in a given IANA timezone (falls back to the
  // browser's local zone if the name is missing/invalid) — used so times
  // match the org-wide Settings timezone, not each viewer's own device.
  function trackTime(isoTimestamp, timeZone) {
    if (!isoTimestamp) return "—";
    const date = new Date(isoTimestamp);
    if (Number.isNaN(date.getTime())) return "—";
    const options = {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    };
    try {
      return date.toLocaleString("en-GB", { ...options, timeZone: timeZone || undefined });
    } catch (invalidTimeZone) {
      return date.toLocaleString("en-GB", options);
    }
  }

  // Seconds -> "4h 30m" (matches apps.core.templatetags.fms_extras.duration_display
  // used server-side, so a duration reads the same whether rendered by Django or by JS).
  function durationText(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(totalSeconds)) return "—";
    const totalMinutes = Math.floor(totalSeconds / 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours && minutes) return `${hours}h ${minutes}m`;
    if (hours) return `${hours}h`;
    return `${minutes}m`;
  }

  global.FmsFormat = { escapeHtml, timeAgo, trackTime, durationText };
})(window);
