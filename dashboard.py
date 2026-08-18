"""Generates a self-contained HTML dashboard from the monitor's Google Sheet.

Runs LOCALLY only — never in CI. The output carries client names and volumes,
and this repo is public, so the file is gitignored and shared by hand (drop it
in the Drive folder where the Sheet already lives; access control is Drive's).

Design rules that matter:
- The page must never contradict a Slack alert, so every colour decision comes
  from src/baseline.py — the same functions main.py runs — and mirrors the same
  eligibility rules (an event the monitor doesn't judge daily isn't judged here).
- One self-contained file: charts are SVG generated here, no CDN, no chart
  library, no network at view time. It opens on any machine, offline.

Usage:
    python dashboard.py [--sheet-id ID] [--out DIR] [--no-open]

The Sheet id comes from --sheet-id, GOOGLE_SHEET_ID in the environment, or a
local .env file. First run opens a browser once to authorise read-only access
and caches the token in token.json (gitignored); after that it asks nothing.
"""
import argparse
import html
import os
import re
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from main import STABLE_DAY_OFFSET, TRUTHY, get_event_config
from src.baseline import (
    BASELINE_DAYS,
    BASELINE_MIN_MEDIAN,
    short_check_status,
    weekday_median,
    window_count,
)
from src.sheets import CONFIG_TAB, DAILY_TAB, PARAMS_TAB, RESULTS_TAB, read_config

# Read-only: this script must never be able to change the Sheet.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
CLIENT_SECRETS_FILE = "client_secret.json"
TOKEN_FILE = "token.json"

# config stores GA4/GADS, results and daily_history store the display names
# GA4/GAds. Everything is keyed by the upper form; display names are for humans.
DISPLAY_PLATFORM = {"GA4": "GA4", "GADS": "GAds"}

# A weekday median needs a few same-weekday samples to mean anything. Early days
# in the window have none (the history only starts there), and weekday_median
# returns 0.0 for an empty sample — drawing that would claim "nothing was
# expected here", which is false. Below this many samples the expected value is
# simply undefined and the page says nothing.
MIN_WEEKDAY_SAMPLES = 4

STATUS_RANK = {"FAIL": 0, "WARN": 1, "OK": 2, "NA": 3}
SEVERITY_RANK = {"critical": 0, "secondary": 1}

WEEKDAY_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
MONTH_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
            "jul", "ago", "set", "out", "nov", "dez"]

COLOR = {
    "OK": "#2e7d32",
    "WARN": "#ef6c00",
    "FAIL": "#c62828",
    "NA": "#78909c",
    "PROV": "#b0bec5",
}


# --------------------------------------------------------------------------
# Sheet access
# --------------------------------------------------------------------------

def load_dotenv(path: str = ".env") -> None:
    """Minimal KEY=VALUE reader so no extra dependency is needed."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def build_credentials() -> Credentials:
    """Cached local OAuth: browser consent once, then token.json forever."""
    credentials = None
    if Path(TOKEN_FILE).is_file():
        credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    else:
        if not Path(CLIENT_SECRETS_FILE).is_file():
            sys.exit(
                f"Falta {CLIENT_SECRETS_FILE} nesta pasta — é o mesmo ficheiro "
                "usado pelo setup_oauth.py (descarregado da Google Cloud Console)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
        credentials = flow.run_local_server(port=0)

    Path(TOKEN_FILE).write_text(credentials.to_json())
    return credentials


def platform_key(value) -> str:
    return str(value).strip().upper()


def display_platform(value) -> str:
    key = platform_key(value)
    return DISPLAY_PLATFORM.get(key, str(value).strip())


def read_sheet(sheet_id: str, client: gspread.Client) -> dict:
    """Returns the three tabs the page is built from, already parsed.

    Everything is read UNFORMATTED. A cell the Sheet displays as "1,000" comes
    back as the string "1,000" by default and would parse to nothing — silently
    turning a busy day into a zero, i.e. a red bar on a healthy day. This matters
    more now that the tabs are updated in place: user formatting persists across
    runs instead of being wiped by a fresh grid.
    """
    raw = gspread.utils.ValueRenderOption.unformatted
    sh = client.open_by_key(sheet_id)

    def tab(title: str, hint: str):
        try:
            return sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            sys.exit(f"A Sheet não tem a aba '{title}' — corre o {hint} primeiro.")

    results = tab(RESULTS_TAB, "Daily Tracking Monitor").get_all_records(
        value_render_option=raw)
    history_values = tab(DAILY_TAB, "Daily Tracking Monitor").get_values(
        value_render_option=raw)
    # Touch the config tab through the same guard: read_config would CREATE it
    # (and write headers) if absent — this script must never write.
    tab(CONFIG_TAB, "Daily Tracking Monitor")

    # Optional, unlike the three tabs above: the params-check feature may not
    # have run yet (dashboard used before the first post-deploy weekly
    # analysis). Its absence just means no "Parâmetros" section anywhere,
    # never a crash.
    try:
        params_analysis = sh.worksheet(PARAMS_TAB).get_all_records(value_render_option=raw)
    except gspread.exceptions.WorksheetNotFound:
        print(f"Aviso: a aba '{PARAMS_TAB}' ainda não existe — a secção "
              "\"Parâmetros\" não aparece (corre o 90-Day History Analysis "
              "com o código de verificação de parâmetros).")
        params_analysis = []

    dates, series = parse_daily_history(history_values)
    return {
        "config": read_config(sheet_id, client),
        "results": results,
        "dates": dates,
        "series": series,
        "params_analysis": params_analysis,
    }


def parse_daily_history(values: list[list]) -> tuple[list[str], dict[str, dict[str, float]]]:
    """daily_history is one row per date, one column per 'client|Platform|event'.

    Cells arrive unformatted, so counts are already numbers; strings are still
    accepted so the parser doesn't depend on the render option. A cell that
    can't be read is reported rather than silently treated as zero — a phantom
    zero would show as a failed day on the page.
    """
    if not values or len(values) < 2:
        return [], {}

    labels = [str(label).strip() for label in values[0][1:]]
    dates: list[str] = []
    series: dict[str, dict[str, float]] = {label: {} for label in labels}
    unreadable = 0

    skipped = []
    for row in values[1:]:
        day = str(row[0]).strip() if row else ""
        if not day:
            continue
        # The date column is a Sheet column a human can edit. Everything
        # downstream does date arithmetic on it, so reject non-ISO rows here
        # instead of letting a stray value crash the whole page.
        try:
            date.fromisoformat(day)
        except ValueError:
            skipped.append(day)
            continue
        dates.append(day)
        for index, label in enumerate(labels, start=1):
            cell = row[index] if index < len(row) else ""
            if cell == "" or cell is None:
                continue
            try:
                series[label][day] = float(cell)
            except (TypeError, ValueError):
                unreadable += 1

    if skipped:
        print(f"Aviso: {len(skipped)} linha(s) da aba '{DAILY_TAB}' têm uma data "
              f"inválida e foram ignoradas: {skipped[:3]}")
    if unreadable:
        print(f"Aviso: {unreadable} célula(s) da aba '{DAILY_TAB}' não foram lidas "
              "como número e ficaram de fora — os dias afetados aparecem a zero.")

    return dates, series


# --------------------------------------------------------------------------
# View model (pure)
# --------------------------------------------------------------------------

def flagged_events(config_rows: list[dict], client_id: str, platform: str) -> set:
    """Events explicitly flagged 24hBackGA4_48hBackGAds='sim' — same rule as main.py."""
    return {
        row["event_name"] for row in config_rows
        if row["client_id"] == client_id
        and platform_key(row["platform"]) == platform
        and str(row.get("24hBackGA4_48hBackGAds", "")).strip().lower() in TRUTHY
    }


def day_status(count: float, expected: float, threshold: float, judged: bool,
               is_flagged: bool, flagged_window: float = None) -> str:
    """Colour for one day, mirroring main.py's short-check eligibility.

    The monitor only judges a day when the event clears the volume floor
    (automatic baseline) or was explicitly flagged. Below that, day-to-day noise
    makes any verdict meaningless — so the page shows the bar without a verdict
    rather than inventing a red the Slack alert would never send.

    `flagged_window` is the count over the platform's stable window
    (`STABLE_DAY_OFFSET`), not the single day: for Google Ads the monitor sums
    TWO days precisely because a conversion can land a day late. Judging a
    flagged GAds day on its own would paint reds the alert never sent.
    """
    if judged:
        return short_check_status(count, expected, threshold)
    if is_flagged:
        total = count if flagged_window is None else flagged_window
        return "OK" if total > 0 else "FAIL"
    return "NA"


def badge_for(row: dict) -> dict:
    """Turns a results row into a human-readable badge."""
    window = str(row.get("window", "")).strip()
    check = str(row.get("check", "count")).strip() or "count"
    status = str(row.get("status", "")).strip() or "OK"
    count = row.get("count", "")
    expected = row.get("expected", "")

    if check == "param":
        # count = fires that carried the parameter; expected = total fires of
        # the event in the same window — "how many should have carried it".
        param_name = str(row.get("param", "")).strip()
        label = f"Parâmetro · {param_name}" if param_name else "Parâmetro"
        has_total = expected not in ("", 0)
        if status == "FAIL":
            detail = f"ausente nos últimos {window}" + (f" ({expected} disparos)" if has_total else "")
        else:
            detail = f"{count}/{expected} disparos" if has_total else f"{count} disparos"
        return {"label": label, "status": status, "detail": detail}

    if check == "value":
        label = f"Valor · {window}"
    elif window.endswith("d"):
        label = f"Janela {window}"
    else:
        label = f"Últimas {window}"

    if status == "WARN" and check == "count" and window.endswith("d") and expected != "":
        # Record-silence WARN: `expected` carries the longest historical gap,
        # not a baseline. Same reading as the Slack message.
        detail = f"silêncio recorde (máx. histórico {expected}d)"
    elif status in ("WARN", "FAIL") and expected != "":
        detail = f"{count} vs {expected} esperados"
    elif status == "FAIL":
        detail = f"{count} no período"
    else:
        detail = str(count)

    return {"label": label, "status": status, "detail": detail}


def build_params_by_event(params_analysis: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    """Groups params_analysis rows by (client_id, "GA4", event_name).

    GAds rows exist in the tab for honesty on the Sheet (every param is
    "não verificável" there) but are dropped here — see the "só GA4" decision
    in the plan: repeating "não verificável" on every GAds event would be
    noise with no action attached to it.

    "não registado no GA4" sorts first within each event: it's the one state
    that's actually actionable (create a custom dimension), so it should be
    the first thing read, not buried after the params that are already fine.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in params_analysis:
        if platform_key(row.get("platform", "")) != "GA4":
            continue
        key = (str(row.get("client_id", "")).strip(), "GA4",
               str(row.get("event_name", "")).strip())
        grouped.setdefault(key, []).append({
            "name": str(row.get("param", "")).strip(),
            "estado": str(row.get("estado", "")).strip(),
            "pct": str(row.get("pct_disparos_com_param", "")).strip(),
            "tag_gtm": str(row.get("tag_gtm", "")).strip(),
        })
    for items in grouped.values():
        items.sort(key=lambda p: (0 if "não registado" in p["estado"] else 1, p["name"]))
    return grouped


def build_view_model(config_rows: list[dict], results: list[dict], dates: list[str],
                     series: dict[str, dict[str, float]], params_analysis: list[dict] = None) -> dict:
    """Everything the page needs, computed from the Sheet. No I/O, no rendering."""
    checked_at = ""
    for row in results:
        if row.get("checked_at"):
            checked_at = str(row["checked_at"])
            break

    params_analysis = params_analysis or []
    params_by_event = build_params_by_event(params_analysis)
    params_analyzed_at = ""
    for row in params_analysis:
        if row.get("analyzed_at"):
            params_analyzed_at = str(row["analyzed_at"])
            break

    # Group results rows by event.
    rows_by_event: dict[tuple, list[dict]] = {}
    for row in results:
        key = (str(row.get("client_id", "")).strip(),
               platform_key(row.get("platform", "")),
               str(row.get("event_name", "")).strip())
        rows_by_event.setdefault(key, []).append(row)

    # Series present in daily_history, keyed the same way.
    series_by_event: dict[tuple, dict[str, float]] = {}
    for label, day_map in series.items():
        parts = label.split("|")
        if len(parts) != 3:
            continue
        client_id, platform, event_name = (p.strip() for p in parts)
        series_by_event[(client_id, platform_key(platform), event_name)] = day_map

    configured = {
        (row["client_id"], platform_key(row["platform"]), row["event_name"])
        for row in config_rows
    }

    week_dates = dates[-7:]
    all_keys = set(rows_by_event) | set(series_by_event) | configured

    clients: dict[str, dict] = {}
    # Counted over ALL severities, with the critical subset alongside, so the
    # header can never say "no alerts" while a client row shows failures.
    totals = {"FAIL": 0, "WARN": 0, "FAIL_critical": 0, "WARN_critical": 0}

    for key in sorted(all_keys):
        client_id, platform, event_name = key
        if not client_id or not event_name:
            continue

        event_rows = rows_by_event.get(key, [])
        day_map = series_by_event.get(key, {})
        cfg = get_event_config(config_rows, client_id, platform, event_name)
        is_flagged = event_name in flagged_events(config_rows, client_id, platform)
        threshold = cfg["threshold"]

        badges = [badge_for(row) for row in event_rows]
        statuses = [b["status"] for b in badges]
        # No results row means the last run didn't verify this event (added to
        # config afterwards, or its fetch failed). Unverified must never read as
        # passing, so it gets its own state rather than defaulting to OK.
        worst = "OK" if badges else "NA"
        for candidate in ("FAIL", "WARN"):
            if candidate in statuses:
                worst = candidate
                break

        severity = cfg["severity"]
        if worst in totals:
            totals[worst] += 1
            if severity == "critical":
                totals[f"{worst}_critical"] += 1

        # Per-day verdicts over the whole window, using the monitor's own rules.
        provisional = set(dates[len(dates) - STABLE_DAY_OFFSET[platform] + 1:]) \
            if STABLE_DAY_OFFSET.get(platform, 1) > 1 else set()

        span = STABLE_DAY_OFFSET.get(platform, 1)
        counts, expecteds, day_statuses = [], [], []
        for i, day in enumerate(dates):
            day_obj = date.fromisoformat(day)
            count = day_map.get(day, 0.0)
            # Only i // 7 same-weekday samples exist before day i INSIDE the
            # window. weekday_median's default reaches 90 days back, so for early
            # days most samples fall outside the loaded history and count as
            # real zeros — dragging the median down and drawing a ramp that
            # claims volume was expected to be low. Cap the lookback to the
            # samples that actually exist. At the last day 7*12 selects exactly
            # the same 12 samples as the default, so this never diverges from
            # the value main.py uses to alert.
            samples = i // 7
            expected = (weekday_median(day_map, day_obj,
                                       lookback_days=min(7 * samples, BASELINE_DAYS))
                        if samples >= MIN_WEEKDAY_SAMPLES else None)
            judged = expected is not None and expected >= BASELINE_MIN_MEDIAN
            # The flagged zero-check in main.py evaluates the window ENDING at
            # the most recent date, so for Google Ads a day is always judged
            # together with the day after it — that is the whole point of the
            # 48h window: a conversion attributed a day late still counts.
            # Judging a day on its own, or on the window ending at it, would
            # paint reds for exactly the case the monitor was built to forgive.
            window_end = dates[min(i + span - 1, len(dates) - 1)]
            status = day_status(
                count, expected or 0.0, threshold, judged, is_flagged,
                flagged_window=window_count(day_map, date.fromisoformat(window_end), span),
            )
            if day in provisional:
                status = "PROV"
            counts.append(count)
            expecteds.append(expected)
            day_statuses.append(status)

        offset = len(dates) - len(week_dates)
        week = [
            {
                "date": day,
                "count": counts[offset + i],
                "expected": expecteds[offset + i] or 0.0,
                "status": day_statuses[offset + i],
            }
            for i, day in enumerate(week_dates)
        ]

        # Today's verdict for each checked parameter, straight from the
        # results rows already grouped for this event — no re-parsing of
        # badge labels. Feeds event["params"]["daily_status"] below.
        param_status_by_name: dict[str, str] = {}
        for row in event_rows:
            if str(row.get("check", "")).strip() != "param":
                continue
            name = str(row.get("param", "")).strip()
            row_status = str(row.get("status", "")).strip()
            if (name not in param_status_by_name
                    or STATUS_RANK.get(row_status, 9) < STATUS_RANK.get(param_status_by_name[name], 9)):
                param_status_by_name[name] = row_status

        event_params = []
        if platform == "GA4":
            for p in params_by_event.get(key, []):
                event_params.append({**p, "daily_status": param_status_by_name.get(p["name"])})

        event = {
            "event_name": event_name,
            "severity": severity,
            "status": worst,
            "in_config": key in configured,
            "badges": badges,
            "threshold_pct": round(threshold * 100),
            "judged": any(e is not None and e >= BASELINE_MIN_MEDIAN for e in expecteds[-7:]),
            "flagged": is_flagged,
            "has_history": bool(day_map),
            # Data, not markup: the view model stays renderer-agnostic and the
            # chart functions are called by render_event.
            "counts": counts,
            "expecteds": expecteds,
            "day_statuses": day_statuses,
            "week": week,
            "params": event_params,
        }

        client = clients.setdefault(client_id, {"client_id": client_id, "platforms": {},
                                                "FAIL": 0, "WARN": 0})
        if worst in ("FAIL", "WARN"):
            client[worst] += 1
        client["platforms"].setdefault(display_platform(platform), []).append(event)

    def sort_key(event: dict) -> tuple:
        return (STATUS_RANK.get(event["status"], 9),
                SEVERITY_RANK.get(event["severity"], 9),
                event["event_name"].lower())

    ordered_clients = []
    for client_id in sorted(clients):
        client = clients[client_id]
        platforms = [
            {"platform": name, "events": sorted(events, key=sort_key)}
            for name, events in sorted(client["platforms"].items())
        ]
        ordered_clients.append({
            "client_id": client_id,
            "fail": client["FAIL"],
            "warn": client["WARN"],
            "platforms": platforms,
        })

    return {
        "checked_at": checked_at,
        "dates": dates,
        "history_last_date": dates[-1] if dates else "",
        "history_days": len(dates),
        "params_analyzed_at": params_analyzed_at,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "totals": totals,
        "clients": ordered_clients,
    }


# --------------------------------------------------------------------------
# SVG charts (pure: numbers in, string out)
# --------------------------------------------------------------------------

def _points(values: list[float], ymax: float, left: int, top: int,
            plot_w: float, plot_h: float) -> list[tuple[float, float]]:
    n = len(values)
    if n == 0:
        return []
    step = plot_w / (n - 1) if n > 1 else 0
    return [
        (left + i * step, top + plot_h - (value / ymax) * plot_h if ymax else top + plot_h)
        for i, value in enumerate(values)
    ]


def line_chart(dates: list[str], counts: list[float], expecteds: list,
               statuses: list[str], width: int = 640, height: int = 132) -> str:
    """90-day line: real counts solid, the expected weekday median dashed.

    Autoscaled per event — a shared scale would flatten every low-volume event
    into a straight line at the bottom. `expecteds` may hold None for the early
    days where no weekday baseline exists yet; those are simply not drawn.
    """
    if not dates:
        return '<div class="empty">sem histórico nesta série</div>'

    left, right, top, bottom = 38, 8, 10, 20
    plot_w = width - left - right
    plot_h = height - top - bottom
    known = [e for e in expecteds if e is not None]
    ymax = max(max(counts, default=0), max(known, default=0), 1)

    real = _points(counts, ymax, left, top, plot_w, plot_h)
    expected = _points([e if e is not None else 0.0 for e in expecteds],
                       ymax, left, top, plot_w, plot_h)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Histórico de {len(dates)} dias">']

    # Month boundaries as light guides — 90 date labels would be unreadable.
    # Dates come from a Sheet column a human can edit, so parse defensively.
    for i, day in enumerate(dates):
        if re.fullmatch(r"\d{4}-\d{2}-01", day):
            x = real[i][0]
            month = MONTH_PT[int(day[5:7]) - 1]
            parts.append(f'<line class="guide" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}"/>')
            parts.append(f'<text class="axis" x="{x:.1f}" y="{height - 6}" text-anchor="middle">{month}</text>')

    parts.append(f'<line class="axis-line" x1="{left}" y1="{top + plot_h}" '
                 f'x2="{left + plot_w}" y2="{top + plot_h}"/>')
    parts.append(f'<text class="axis" x="{left - 5}" y="{top + 5}" text-anchor="end">{_num(ymax)}</text>')
    parts.append(f'<text class="axis" x="{left - 5}" y="{top + plot_h}" text-anchor="end">0</text>')

    # Only over the stretch where a weekday baseline actually exists.
    defined = [i for i, e in enumerate(expecteds) if e is not None]
    if len(defined) > 1 and any(known):
        path = " ".join(f"{expected[i][0]:.1f},{expected[i][1]:.1f}" for i in defined)
        parts.append(f'<polyline class="expected" points="{path}"/>')

    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in real)
    parts.append(f'<polyline class="real" points="{path}"/>')

    # Only flagged days get a marker, so past incidents stand out at a glance.
    for i, status in enumerate(statuses):
        if status not in ("WARN", "FAIL"):
            continue
        x, y = real[i]
        expectation = "" if expecteds[i] is None else f" (esperado {_num(expecteds[i])})"
        parts.append(
            f'<circle class="pt {status}" cx="{x:.1f}" cy="{y:.1f}" r="2.8">'
            f'<title>{esc(dates[i])} · {_num(counts[i])}{expectation}</title></circle>'
        )

    parts.append("</svg>")
    return "".join(parts)


def bar_chart(week: list[dict], width: int = 340, height: int = 132) -> str:
    """Last 7 days as bars, coloured by the monitor's own verdict."""
    if not week:
        return '<div class="empty">sem dados da semana</div>'

    left, right, top, bottom = 8, 8, 16, 20
    plot_w = width - left - right
    plot_h = height - top - bottom
    ymax = max([d["count"] for d in week] + [d["expected"] for d in week] + [1])

    slot = plot_w / len(week)
    bar_w = slot * 0.62

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Últimos 7 dias">']
    parts.append(f'<line class="axis-line" x1="{left}" y1="{top + plot_h}" '
                 f'x2="{left + plot_w}" y2="{top + plot_h}"/>')

    for i, day in enumerate(week):
        cx = left + slot * i + slot / 2
        x = cx - bar_w / 2
        bar_h = (day["count"] / ymax) * plot_h
        y = top + plot_h - bar_h
        weekday = WEEKDAY_PT[date.fromisoformat(day["date"]).weekday()]

        # A day with zero events is the single most important thing this chart
        # can show, and a zero-height bar shows nothing. Draw a visible stub in
        # a distinct style so "no events" never reads as "empty chart".
        is_zero = day["count"] <= 0
        klass = f'bar {day["status"]}' + (" zero" if is_zero else "")
        if is_zero:
            bar_h = 5.0
            y = top + plot_h - bar_h

        parts.append(
            f'<rect class="{klass}" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{max(bar_h, 1.5):.1f}" rx="1.5">'
            f'<title>{esc(day["date"])} · {_num(day["count"])} '
            f'(esperado {_num(day["expected"])})</title></rect>'
        )

        if day["expected"] > 0:
            ey = top + plot_h - (day["expected"] / ymax) * plot_h
            parts.append(f'<line class="expected-tick" x1="{x - 2:.1f}" y1="{ey:.1f}" '
                         f'x2="{x + bar_w + 2:.1f}" y2="{ey:.1f}"/>')

        parts.append(f'<text class="value" x="{cx:.1f}" y="{max(y - 4, 10):.1f}" '
                     f'text-anchor="middle">{_num(day["count"])}</text>')
        parts.append(f'<text class="axis" x="{cx:.1f}" y="{height - 6}" '
                     f'text-anchor="middle">{weekday}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _num(value: float) -> str:
    if value is None:
        return "0"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f4f6f8; --card: #ffffff; --ink: #1f2933; --muted: #6b7785;
  --line: #dfe4ea; --ok: #2e7d32; --warn: #ef6c00; --fail: #c62828;
  --na: #78909c; --prov: #b0bec5; --accent: #1a73e8;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 24px 20px 64px; }

header { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px 22px; margin-bottom: 20px; }
h1 { margin: 0 0 4px; font-size: 20px; }
.stamp { font-size: 15px; font-weight: 600; color: var(--ink); margin: 10px 0 2px; }
.stamp small { display: block; font-weight: 400; color: var(--muted); font-size: 12.5px; }
.tally { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.pill { border-radius: 999px; padding: 4px 12px; font-size: 12.5px; font-weight: 600; }
.pill.FAIL { background: #ffebee; color: var(--fail); }
.pill.WARN { background: #fff3e0; color: var(--warn); }
.pill.OK { background: #e8f5e9; color: var(--ok); }

.controls { display: flex; gap: 12px; flex-wrap: wrap; align-items: center;
  margin: 16px 0 20px; }
.controls input[type=search] { flex: 1; min-width: 200px; padding: 8px 12px;
  border: 1px solid var(--line); border-radius: 8px; font-size: 13.5px; background: var(--card); }
.controls label { font-size: 13px; color: var(--muted); display: flex; gap: 6px; align-items: center; }

details.client { background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; margin-bottom: 14px; overflow: hidden; }
details.client > summary { cursor: pointer; padding: 14px 18px; font-size: 16px;
  font-weight: 600; list-style: none; display: flex; gap: 10px; align-items: center; }
details.client > summary::-webkit-details-marker { display: none; }
details.client > summary::before { content: "▸"; color: var(--muted); font-size: 12px; }
details.client[open] > summary::before { content: "▾"; }
.platform { padding: 0 18px; }
.platform h3 { font-size: 12px; letter-spacing: .07em; font-weight: 700;
  color: var(--muted); margin: 14px 0 8px; }

.event { border-top: 1px solid var(--line); padding: 14px 0; }
.event.hidden { display: none; }
.event-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.event-name { font-weight: 600; font-size: 14.5px; }
.tag { font-size: 11px; padding: 2px 7px; border-radius: 4px; background: #eef1f4;
  color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.tag.critical { background: #ede7f6; color: #5e35b1; }
.badge { font-size: 12px; padding: 3px 9px; border-radius: 6px; border: 1px solid transparent; }
.badge.OK { background: #e8f5e9; color: var(--ok); }
.badge.WARN { background: #fff3e0; color: var(--warn); }
.badge.FAIL { background: #ffebee; color: var(--fail); }
.badge.NA { background: #eceff1; color: var(--muted); }

.params { margin-top: 14px; padding-top: 10px; border-top: 1px dashed var(--line); }
.params h4 { margin: 0 0 8px; font-size: 12px; color: var(--muted); font-weight: 600; }
.param-row { display: flex; align-items: center; gap: 10px; padding: 3px 0; font-size: 13px; }
.param-name { font-weight: 600; min-width: 150px; }

.charts { display: flex; gap: 20px; flex-wrap: wrap; align-items: flex-start; }
.chart-box { flex: 1 1 320px; min-width: 280px; }
.chart-box h4 { margin: 0 0 2px; font-size: 12px; color: var(--muted); font-weight: 600; }
.chart-box p { margin: 2px 0 0; font-size: 11.5px; color: var(--muted); }
svg.chart { width: 100%; height: auto; display: block; }
.empty { font-size: 12px; color: var(--muted); padding: 20px 0; }

.real { fill: none; stroke: var(--accent); stroke-width: 1.7; stroke-linejoin: round; }
.expected { fill: none; stroke: #9aa0a6; stroke-width: 1.2; stroke-dasharray: 3 3; }
.expected-tick { stroke: #5f6368; stroke-width: 1.1; stroke-dasharray: 2 2; }
.guide { stroke: #eceff1; stroke-width: 1; }
.axis-line { stroke: var(--line); stroke-width: 1; }
text.axis { font-size: 9.5px; fill: var(--muted); }
text.value { font-size: 10px; fill: var(--ink); font-weight: 600; }
.pt.WARN { fill: var(--warn); }
.pt.FAIL { fill: var(--fail); }
rect.bar.OK { fill: var(--ok); }
rect.bar.WARN { fill: var(--warn); }
rect.bar.FAIL { fill: var(--fail); }
rect.bar.NA { fill: var(--na); opacity: .6; }
rect.bar.PROV { fill: var(--prov); }
rect.bar.zero { fill-opacity: .3; stroke-width: 1.2; stroke-dasharray: 3 2; }
rect.bar.zero.OK, rect.bar.zero.NA { stroke: var(--na); }
rect.bar.zero.WARN { stroke: var(--warn); }
rect.bar.zero.FAIL { stroke: var(--fail); }
rect.bar.zero.PROV { stroke: #90a4ae; }

footer { margin-top: 32px; padding: 18px 20px; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; font-size: 12.5px; color: var(--muted); }
footer h4 { margin: 0 0 8px; font-size: 13px; color: var(--ink); }
footer li { margin-bottom: 5px; }
footer ul { margin: 0 0 12px; padding-left: 18px; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 5px; vertical-align: baseline; }
"""

JS = """
(function () {
  var search = document.getElementById('search');
  var onlyIssues = document.getElementById('only-issues');
  var showAll = document.getElementById('show-all');

  function apply() {
    var term = search.value.trim().toLowerCase();
    document.querySelectorAll('.event').forEach(function (el) {
      var matchesTerm = !term || el.dataset.name.indexOf(term) !== -1;
      var matchesIssue = !onlyIssues.checked || el.dataset.status === 'FAIL' || el.dataset.status === 'WARN';
      var matchesScope = showAll.checked || el.dataset.config === '1';
      el.classList.toggle('hidden', !(matchesTerm && matchesIssue && matchesScope));
    });
    document.querySelectorAll('details.client').forEach(function (box) {
      var visible = box.querySelectorAll('.event:not(.hidden)').length;
      box.style.display = visible ? '' : 'none';
    });
  }

  [search, onlyIssues, showAll].forEach(function (el) {
    el.addEventListener('input', apply);
    el.addEventListener('change', apply);
  });
  apply();
})();
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def render_params(params: list[dict]) -> str:
    """One row per verified parameter. `title=` carries the raw estado + GTM
    tag as a native tooltip, same pattern as the SVG charts' hover text."""
    rows = []
    for p in params:
        if "não registado" in p["estado"]:
            status, detail = "WARN", "não registado no GA4 — criar custom dimension"
        elif p["daily_status"] == "FAIL":
            status, detail = "FAIL", f"ausente hoje (histórico: {p['pct'] or '—'} dos disparos em 90d)"
        elif p["daily_status"] in ("WARN", "OK"):
            status, detail = p["daily_status"], f"presente em {p['pct'] or '—'} dos disparos (90d)"
        else:
            # Not watched daily: secondary event, or history doesn't yet
            # prove the param is sent consistently enough to alert on.
            status, detail = "NA", f"presente em {p['pct'] or '—'} dos disparos (90d) · não vigiado diariamente"
        rows.append(
            f'<div class="param-row" title="{esc(p["estado"])} · tag GTM: {esc(p["tag_gtm"] or "—")}">'
            f'<span class="param-name">{esc(p["name"])}</span>'
            f'<span class="badge {status}">{esc(detail)}</span></div>'
        )
    return "".join(rows)


def render_event(event: dict, dates: list[str]) -> str:
    badges = "".join(
        f'<span class="badge {esc(b["status"])}">{esc(b["label"])} · '
        f'{esc(b["status"])} · {esc(b["detail"])}</span>'
        for b in event["badges"]
    ) or '<span class="badge OK">sem verificação registada hoje</span>'

    if event["judged"]:
        note = (f'Verde/amarelo/vermelho pela mediana do dia-da-semana, '
                f'com o limiar de {event["threshold_pct"]}%.')
    elif event["flagged"]:
        note = "Marcado 24h/48h na config: vermelho significa zero eventos nesse dia."
    else:
        note = ("Volume baixo demais para julgar um dia isolado — o monitor também não "
                "o julga, por isso as barras ficam neutras.")

    scope = "1" if event["in_config"] else "0"
    tag_extra = "" if event["in_config"] else '<span class="tag">fora da config</span>'

    params_html = ""
    if event.get("params"):
        params_html = f"""
        <div class="params">
          <h4>Parâmetros</h4>
          {render_params(event["params"])}
        </div>"""

    return f"""
      <div class="event" data-name="{esc(event["event_name"].lower())}"
           data-status="{esc(event["status"])}" data-config="{scope}">
        <div class="event-head">
          <span class="event-name">{esc(event["event_name"])}</span>
          <span class="tag {esc(event["severity"])}">{esc(event["severity"])}</span>
          {tag_extra}
          {badges}
        </div>
        <div class="charts">
          <div class="chart-box">
            <h4>Histórico — contagem por dia</h4>
            {line_chart(dates, event["counts"], event["expecteds"], event["day_statuses"])}
            <p>Linha cheia = real · linha tracejada = esperado para aquele dia-da-semana.
               Pontos marcam dias com alerta.</p>
          </div>
          <div class="chart-box">
            <h4>Últimos 7 dias</h4>
            {bar_chart(event["week"])}
            <p>{esc(note)}</p>
          </div>
        </div>
        {params_html}
      </div>"""


def render_html(view: dict) -> str:
    clients_html = []
    for client in view["clients"]:
        has_issues = client["fail"] or client["warn"]
        pills = []
        if client["fail"]:
            pills.append(f'<span class="pill FAIL">{client["fail"]} FAIL</span>')
        if client["warn"]:
            pills.append(f'<span class="pill WARN">{client["warn"]} WARN</span>')
        if not pills:
            pills.append('<span class="pill OK">tudo OK</span>')

        platforms = []
        for platform in client["platforms"]:
            events = "".join(render_event(event, view["dates"]) for event in platform["events"])
            platforms.append(
                f'<div class="platform"><h3>{esc(platform["platform"])}</h3>{events}</div>'
            )

        clients_html.append(f"""
    <details class="client" {"open" if has_issues else ""}>
      <summary>{esc(client["client_id"])} {"".join(pills)}</summary>
      {"".join(platforms)}
    </details>""")

    totals = view["totals"]
    tally = []
    for state in ("FAIL", "WARN"):
        if totals[state]:
            critical = totals[f"{state}_critical"]
            tally.append(f'<span class="pill {state}">{totals[state]} {state}'
                         f' · {critical} critical</span>')
    if not tally:
        tally.append('<span class="pill OK">sem alertas</span>')

    return f"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tracking Monitor — {esc(view["checked_at"][:10])}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Estado do tracking</h1>
    <div class="stamp">Última verificação: {esc(view["checked_at"] or "desconhecida")}
      <small>Histórico até {esc(view["history_last_date"] or "—")}
      ({view["history_days"]} dias)
      {f' · Verificação de parâmetros: {esc(view["params_analyzed_at"])}' if view.get("params_analyzed_at") else ""}
      · página gerada em {esc(view["generated_at"])}</small>
    </div>
    <div class="tally">{"".join(tally)}</div>
  </header>

  <div class="controls">
    <input type="search" id="search" placeholder="Procurar evento…">
    <label><input type="checkbox" id="only-issues"> só FAIL e WARN</label>
    <label><input type="checkbox" id="show-all"> incluir eventos fora da config</label>
  </div>

  {"".join(clients_html)}

  <footer>
    <h4>Como ler esta página</h4>
    <ul>
      <li><span class="swatch" style="background:#2e7d32"></span><b>Verde</b> — dentro do esperado.</li>
      <li><span class="swatch" style="background:#ef6c00"></span><b>Amarelo</b> — abaixo do limiar face à mediana daquele dia-da-semana.</li>
      <li><span class="swatch" style="background:#c62828"></span><b>Vermelho</b> — zero eventos nesse dia.</li>
      <li><span class="swatch" style="background:#78909c;opacity:.45"></span><b>Cinzento claro</b> — evento de volume baixo: o monitor não emite veredito diário, logo a página também não.</li>
      <li><span class="swatch" style="background:#b0bec5"></span><b>Cinzento</b> — provisório. No Google Ads uma conversão pode demorar 24-72h a ser atribuída, por isso o dia mais recente nunca é dado como falhado (é o mesmo dia que o monitor ignora nas verificações).</li>
    </ul>
    <p><b>Secção "Parâmetros"</b> (só em eventos GA4 — Google Ads não expõe parâmetros de conversão): mostra se o que a tag GTM está configurada a enviar chega mesmo preenchido à GA4, não só o que está configurado. "Não registado no GA4" é uma to-do list — falta criar uma custom dimension para o poder verificar. É atualizada semanalmente, por isso tem o seu próprio carimbo de data no topo, distinto do resto da página.</p>
    <p>Os critérios de cor são os mesmos que geram os alertas do Slack (importados de <code>src/baseline.py</code>),
       por isso esta página e o alerta nunca se contradizem.</p>
    <p><b>Isto não é ao vivo.</b> Mostra o que a Google Sheet tinha na última corrida do monitor — confirma sempre a data no topo.</p>
  </footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gera o dashboard HTML local.")
    parser.add_argument("--sheet-id", default="", help="Id da Google Sheet do monitor.")
    parser.add_argument("--out", default=".", help="Pasta onde gravar o ficheiro.")
    parser.add_argument("--no-open", action="store_true", help="Não abrir o browser no fim.")
    args = parser.parse_args()

    load_dotenv()
    sheet_id = args.sheet_id or os.environ.get("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        sys.exit("Falta o id da Sheet: usa --sheet-id, ou define GOOGLE_SHEET_ID "
                 "no ambiente ou num ficheiro .env local.")

    print("A autenticar…")
    client = gspread.authorize(build_credentials())

    print("A ler a Sheet…")
    data = read_sheet(sheet_id, client)
    if not data["dates"]:
        sys.exit(f"A aba '{DAILY_TAB}' está vazia — corre o monitor antes de gerar a página.")
    if not data["config"]:
        print(f"Aviso: a aba '{CONFIG_TAB}' não devolveu linhas — a página fica sem severidades.")

    view = build_view_model(data["config"], data["results"], data["dates"],
                            data["series"], data["params_analysis"])

    # The filename carries the date on purpose: this file gets shared, and two
    # snapshots in the same Drive folder have to be tellable apart. The stamp
    # comes from the Sheet, so it is validated before touching the filesystem.
    stamp = view["checked_at"][:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
        stamp = date.today().isoformat()
    out_path = Path(args.out) / f"dashboard-{stamp}.html"
    out_path.write_text(render_html(view), encoding="utf-8")

    events = sum(len(p["events"]) for c in view["clients"] for p in c["platforms"])
    print(f"Escrito {out_path} — {len(view['clients'])} clientes, {events} eventos, "
          f"{view['totals']['FAIL']} FAIL / {view['totals']['WARN']} WARN critical.")

    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
