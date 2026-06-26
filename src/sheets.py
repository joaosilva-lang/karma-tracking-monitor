from datetime import datetime
import gspread
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/analytics.readonly",
]

CONFIG_TAB = "config"
RESULTS_TAB = "results"

CONFIG_HEADERS = ["client_id", "platform", "event_name", "severity"]
RESULTS_HEADERS = ["checked_at", "client_id", "platform", "event_name", "severity", "count_7d", "status"]


def get_sheets_client(credentials: Credentials) -> gspread.Client:
    return gspread.authorize(credentials)


def read_config(sheet_id: str, client: gspread.Client) -> list[dict]:
    """Returns list of {client_id, platform, event_name, severity} from the config tab."""
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=CONFIG_TAB, rows=100, cols=10)
        ws.append_row(CONFIG_HEADERS)
        return []

    records = ws.get_all_records()
    return [r for r in records if r.get("client_id") and r.get("event_name")]


def write_results(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Appends result rows to the results tab, creating it if needed."""
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(RESULTS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=RESULTS_TAB, rows=1000, cols=10)
        ws.append_row(RESULTS_HEADERS)

    checked_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    for row in rows:
        ws.append_row([
            checked_at,
            row["client_id"],
            row["platform"],
            row["event_name"],
            row["severity"],
            row["count_7d"],
            row["status"],
        ])
