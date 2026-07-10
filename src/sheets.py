from datetime import datetime
import gspread
from google.oauth2.credentials import Credentials

CONFIG_TAB = "config"
RESULTS_TAB = "results"
HISTORY_TAB = "history_analysis"
DAILY_TAB = "daily_history"

# Optional columns: baseline_threshold_pct is the WARN threshold for the
# relative check as a % of the weekday median (empty -> 50%);
# gtm_container_id (GTM-XXXXXX, one non-empty value per client is enough) and
# Nome_Tag_GTM (filled by the analyze_history GTM step, never by hand) drive
# the informational event->GTM-tag mapping.
CONFIG_HEADERS = [
    "client_id", "account_id", "platform", "event_name",
    "severity", "goback_days", "24hBackGA4_48hBackGAds", "baseline_threshold_pct",
    "gtm_container_id", "Nome_Tag_GTM",
]
GTM_TAG_COLUMN = "Nome_Tag_GTM"
DEFAULT_GOBACK_DAYS = 7

# `window` distinguishes the short 24h-style check ("24h"/"48h") from the
# wider goback window ("10d", etc). `count` is the event/conversion count in
# that window. `expected` is the weekday-median baseline the count was
# compared against (empty for rows without a relative check).
RESULTS_HEADERS = [
    "checked_at", "client_id", "platform", "event_name",
    "severity", "window", "count", "expected", "status",
]

HISTORY_HEADERS = [
    "analyzed_at", "client_id", "platform", "event_name",
    "days_fired_of_90", "pct_days", "avg_per_day",
    "median_per_day", "weekday_medians", "suggestion_24h",
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

    # Only require the headers that actually exist in the Sheet, so adding an
    # optional column here doesn't break runs before the Sheet catches up.
    present = ws.row_values(1)
    expected = [h for h in CONFIG_HEADERS if h in present]
    records = ws.get_all_records(expected_headers=expected)
    return [r for r in records if r.get("client_id") and r.get("event_name")]


def update_config_gtm_tags(sheet_id: str, client: gspread.Client,
                           values_by_key: dict[tuple[str, str, str], str]) -> int:
    """Surgically fills the Nome_Tag_GTM column of the config tab.

    values_by_key: {(client_id, PLATFORM_UPPER, event_name): cell_value}.
    Only cells in that one column are touched — rows are matched in place,
    never created, and no other column is written. Returns how many cells
    were updated; 0 if the column doesn't exist in the Sheet yet.
    """
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(CONFIG_TAB)

    header = ws.row_values(1)
    if GTM_TAG_COLUMN not in header:
        return 0
    tag_col = header.index(GTM_TAG_COLUMN) + 1  # 1-based

    def col_index(name: str) -> int | None:
        return header.index(name) if name in header else None

    ci, pi, ei = col_index("client_id"), col_index("platform"), col_index("event_name")
    if None in (ci, pi, ei):
        return 0

    updates = []
    for row_number, row in enumerate(ws.get_all_values()[1:], start=2):
        def cell(idx):
            return row[idx].strip() if idx < len(row) else ""

        key = (cell(ci), cell(pi).upper(), cell(ei))
        if key in values_by_key:
            updates.append(gspread.Cell(row_number, tag_col, values_by_key[key]))

    if updates:
        ws.update_cells(updates, value_input_option="RAW")
    return len(updates)


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
            row.get("expected", ""),
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
    ws = sh.add_worksheet(title=HISTORY_TAB, rows=max(500, len(rows) + 10), cols=12)

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
            row["median_per_day"],
            row["weekday_medians"],
            row["suggestion_24h"],
        ])
    ws.update(table, value_input_option="RAW")


def write_daily_history(sheet_id: str, client: gspread.Client,
                        dates: list[str], series: dict[str, dict[str, float]]) -> None:
    """Writes the daily_history tab: one row per date, one column per
    (client|platform|event) series.

    Updated IN PLACE (clear + update, never delete + recreate) so native
    Sheets charts the user builds on this tab survive each refresh.
    """
    sh = client.open_by_key(sheet_id)
    labels = sorted(series)
    table = [["date"] + labels]
    for d in dates:
        table.append([d] + [series[label].get(d, 0) for label in labels])

    try:
        ws = sh.worksheet(DAILY_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=DAILY_TAB, rows=len(table) + 10, cols=len(labels) + 5
        )

    if ws.col_count < len(labels) + 1:
        ws.add_cols(len(labels) + 1 - ws.col_count)
    if ws.row_count < len(table):
        ws.add_rows(len(table) - ws.row_count)

    ws.clear()
    ws.update(table, value_input_option="RAW")
