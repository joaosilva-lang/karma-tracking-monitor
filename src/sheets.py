from datetime import datetime
import gspread
from google.oauth2.credentials import Credentials

CONFIG_TAB = "config"
RESULTS_TAB = "results"
HISTORY_TAB = "history_analysis"
DAILY_TAB = "daily_history"
PROPOSAL_TAB = "config_proposta"

# Optional columns: baseline_threshold_pct is the WARN threshold for the
# relative check as a % of the weekday median (empty -> 50%);
# gtm_container_id (GTM-XXXXXX, one non-empty value per client is enough),
# Nome_Tag_GTM and GTM_Event_Params (both filled by the analyze_history GTM
# step, never by hand) drive the informational event->GTM mapping.
CONFIG_HEADERS = [
    "client_id", "account_id", "platform", "event_name",
    "severity", "goback_days", "24hBackGA4_48hBackGAds", "baseline_threshold_pct",
    "gtm_container_id", "Nome_Tag_GTM", "GTM_Event_Params",
]
GTM_TAG_COLUMN = "Nome_Tag_GTM"
GTM_PARAMS_COLUMN = "GTM_Event_Params"
# Written into surplus duplicate config rows when an event has fewer active
# tags than rows (e.g. a tag was paused after the rows were created). Rows are
# never deleted — the marker tells the human which duplicates to clean up.
NO_ACTIVE_TAG = "(sem tag ativa correspondente)"

# Onboarding proposal: the config columns first (in the LIVE config tab's own
# order, resolved at write time — see write_config_proposal) so reviewed rows
# copy-paste straight into config, followed by these informational stats.
STAT_HEADERS = [
    "median_per_day", "pct_days", "pct_days_with_value",
    "value_carrying", "max_gap_days", "goback_days_sugerido",
    "weekday_medians",
]
# Fallback header order used only when the config tab doesn't exist yet.
PROPOSAL_HEADERS = CONFIG_HEADERS + STAT_HEADERS
DEFAULT_GOBACK_DAYS = 7

# `window` distinguishes the short 24h-style check ("24h"/"48h") from the
# wider goback window ("10d", etc). `check` is "count" (event/conversion
# counting) or "value" (monetary value carried by the event — for value rows
# the `count` column holds the value sum). `expected` is the weekday-median
# baseline the count was compared against (empty for rows without a relative
# check).
RESULTS_HEADERS = [
    "checked_at", "client_id", "platform", "event_name", "check",
    "severity", "window", "count", "expected", "status",
]

HISTORY_HEADERS = [
    "analyzed_at", "client_id", "platform", "event_name",
    "days_fired_of_90", "pct_days", "avg_per_day",
    "median_per_day", "weekday_medians",
    "pct_days_with_value", "value_carrying",
    "max_gap_days", "goback_days_sugerido", "suggestion_24h",
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


def plan_config_gtm_updates(header: list[str], data_rows: list[list[str]],
                            values_by_key: dict[tuple[str, str, str], list[dict[str, str]]],
                            ) -> tuple[list[tuple[int, int, str]], list[tuple[int, list[str]]]]:
    """Pure planner for update_config_columns (testable without gspread).

    values_by_key: {(client_id, PLATFORM_UPPER, event_name): [{column: value},
    ...]} — ONE dict per desired row for that event (one tag per row). Rows of
    the same key are matched in sheet order: entry i -> matching row i.
    - More entries than rows: extra entries become new rows inserted below the
      key's last row (a copy of it with the named columns replaced) — the one
      case where the script creates config rows.
    - Fewer entries than rows: surplus rows get NO_ACTIVE_TAG in the named
      columns (never deleted).
    - Keys with no matching row are skipped: config decides what is monitored.

    Returns (cell_updates, row_insertions):
    - cell_updates: [(row_number, col_number, value)] — 1-indexed, coordinates
      valid BEFORE any insertion (apply these first).
    - row_insertions: [(insert_at_row_number, [row_values, ...])] — apply
      bottom-up (descending anchor) so earlier anchors don't shift.
    """
    def col_index(name: str) -> int | None:
        return header.index(name) if name in header else None

    ci, pi, ei = col_index("client_id"), col_index("platform"), col_index("event_name")
    if None in (ci, pi, ei):
        return [], []

    rows_by_key: dict[tuple[str, str, str], list[int]] = {}
    for row_number, row in enumerate(data_rows, start=2):
        def cell(idx):
            return row[idx].strip() if idx < len(row) else ""
        rows_by_key.setdefault((cell(ci), cell(pi).upper(), cell(ei)), []).append(row_number)

    cell_updates: list[tuple[int, int, str]] = []
    row_insertions: list[tuple[int, list[str]]] = []

    for key, entries in values_by_key.items():
        row_numbers = rows_by_key.get(key, [])
        if not row_numbers or not entries:
            continue
        writable_columns = {c for entry in entries for c in entry if col_index(c) is not None}

        # entry i -> row i
        for row_number, entry in zip(row_numbers, entries):
            for column in writable_columns:
                cell_updates.append(
                    (row_number, col_index(column) + 1, entry.get(column, "")))

        # more tags than rows -> insert copies of the last row below it
        if len(entries) > len(row_numbers):
            anchor = row_numbers[-1]
            template = list(data_rows[anchor - 2])
            template += [""] * (len(header) - len(template))
            block = []
            for entry in entries[len(row_numbers):]:
                new_row = list(template)
                for column in writable_columns:
                    new_row[col_index(column)] = entry.get(column, "")
                block.append(new_row)
            row_insertions.append((anchor + 1, block))

        # more rows than tags -> mark the surplus, never delete
        for row_number in row_numbers[len(entries):]:
            for column in writable_columns:
                cell_updates.append((row_number, col_index(column) + 1, NO_ACTIVE_TAG))

    return cell_updates, row_insertions


def update_config_columns(sheet_id: str, client: gspread.Client,
                          values_by_key: dict[tuple[str, str, str], list[dict[str, str]]]) -> int:
    """Fills script-owned columns of the config tab, one tag per row.

    See plan_config_gtm_updates for the matching semantics. Only the named
    columns are touched (and only those present in the Sheet header); the only
    structural change ever made is inserting duplicate rows when an event has
    more active GTM tags than config rows. Returns cells updated + rows added.
    """
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(CONFIG_TAB)

    all_values = ws.get_all_values()
    if not all_values:
        return 0
    header, data_rows = all_values[0], all_values[1:]

    cell_updates, row_insertions = plan_config_gtm_updates(header, data_rows, values_by_key)

    if cell_updates:
        ws.update_cells(
            [gspread.Cell(r, c, v) for r, c, v in cell_updates],
            value_input_option="RAW",
        )
    # Bottom-up so lower anchors aren't shifted by earlier insertions.
    inserted = 0
    for anchor, block in sorted(row_insertions, reverse=True):
        ws.insert_rows(block, row=anchor, value_input_option="RAW")
        inserted += len(block)

    return len(cell_updates) + inserted


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
            row.get("check", "count"),
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
            row["pct_days_with_value"],
            row["value_carrying"],
            row["max_gap_days"],
            row["goback_days_sugerido"],
            row["suggestion_24h"],
        ])
    ws.update(table, value_input_option="RAW")


def write_config_proposal(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Recreates the config_proposta tab with the onboarding proposal.

    The config columns are laid out in the LIVE config tab's own header order
    (read here at write time), so reviewed rows copy-paste straight into config
    without column misalignment — whatever order the user keeps config in. The
    informational stats (STAT_HEADERS) always follow to the right; pasting a
    whole row is safe because those extra columns land under no config header
    and read_config ignores them. Never touches the real config tab.
    """
    sh = client.open_by_key(sheet_id)

    # Mirror config's current column order; fall back to the canonical order
    # only if config doesn't exist yet or is empty.
    try:
        config_header = sh.worksheet(CONFIG_TAB).row_values(1)
    except gspread.exceptions.WorksheetNotFound:
        config_header = []
    if not config_header:
        config_header = list(CONFIG_HEADERS)

    headers = config_header + STAT_HEADERS

    try:
        ws = sh.worksheet(PROPOSAL_TAB)
        sh.del_worksheet(ws)
    except gspread.exceptions.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(
        title=PROPOSAL_TAB,
        rows=max(200, len(rows) + 10),
        cols=len(headers) + 2,
    )

    table = [headers]
    for row in rows:
        table.append([row.get(header, "") for header in headers])
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
