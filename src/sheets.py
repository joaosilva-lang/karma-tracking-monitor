from datetime import datetime
import gspread
from google.oauth2.credentials import Credentials

CONFIG_TAB = "config"
RESULTS_TAB = "results"
HISTORY_TAB = "history_analysis"

CONFIG_HEADERS = [
    "client_id", "account_id", "platform", "event_name",
    "severity", "goback_days", "24hours_lookback",
]
DEFAULT_GOBACK_DAYS = 7

# `window` distinguishes the short 24h-style check ("24h"/"48h") from the
# wider goback window ("10d", etc). `count` is the event/conversion count in
# that window.
RESULTS_HEADERS = [
    "checked_at", "client_id", "platform", "event_name",
    "severity", "window", "count", "status",
]

HISTORY_HEADERS = [
    "analyzed_at", "client_id", "platform", "event_name",
    "days_fired_of_90", "pct_days", "avg_per_day", "suggestion_24h",
]


def get_sheets_client(credentials: Credentials) -> gspread.Client:
    return gspread.authorize(credentials)


def read_config(sheet_id: str, client: gspread.Client) -> list[dict]:
    """Returns config rows from the config tab."""
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=CONFIG_TAB, rows=100, cols=10)
        ws.append_row(CONFIG_HEADERS)
        return []

    records = ws.get_all_records(expected_headers=CONFIG_HEADERS)
    return [r for r in records if r.get("client_id") and r.get("event_name")]


def write_results(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Recreates the results tab (clean history) and writes all result rows."""
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(RESULTS_TAB)
        sh.del_worksheet(ws)
    except gspread.exceptions.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(title=RESULTS_TAB, rows=max(1000, len(rows) + 10), cols=10)

    checked_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    table = [RESULTS_HEADERS]
    for row in rows:
        table.append([
            checked_at,
            row["client_id"],
            row["platform"],
            row["event_name"],
            row["severity"],
            row["window"],
            row["count"],
            row["status"],
        ])
    ws.update(table, value_input_option="RAW")


def write_history_analysis(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Recreates the history_analysis tab with 90-day frequency suggestions."""
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(HISTORY_TAB)
        sh.del_worksheet(ws)
    except gspread.exceptions.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(title=HISTORY_TAB, rows=max(500, len(rows) + 10), cols=10)

    analyzed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    table = [HISTORY_HEADERS]
    for row in rows:
        table.append([
            analyzed_at,
            row["client_id"],
            row["platform"],
            row["event_name"],
            row["days_fired_of_90"],
            row["pct_days"],
            row["avg_per_day"],
            row["suggestion_24h"],
        ])
    ws.update(table, value_input_option="RAW")
