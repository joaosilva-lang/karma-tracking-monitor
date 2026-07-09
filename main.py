import os
from datetime import date, timedelta

from google.oauth2.credentials import Credentials

from src.baseline import (
    BASELINE_DAYS,
    BASELINE_MIN_MEDIAN,
    parse_threshold,
    short_check_status,
    weekday_median,
    window_count,
)
from src.ga4 import fetch_daily_event_counts
from src.gads import fetch_daily_conversion_counts, get_gads_client
from src.sheets import get_sheets_client, read_config, write_results, DEFAULT_GOBACK_DAYS
from src.slack import send_alert

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/adwords",
]

# Most recent *stable* day per platform. GA4 data for yesterday is settled; a
# Google Ads conversion can take 24-72h to be fully attributed, so the short
# check tests the day before yesterday instead of yesterday.
STABLE_DAY_OFFSET = {"GA4": 1, "GADS": 2}
WINDOW_24H_LABEL = {"GA4": "24h", "GADS": "48h"}

TRUTHY = {"sim", "yes", "true", "1", "y", "s"}


def build_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def build_client_accounts(config_rows: list[dict]) -> dict:
    """Returns {client_id: {platform: account_id}} derived from Sheet config rows."""
    accounts: dict[str, dict[str, str]] = {}
    for row in config_rows:
        client_id = row["client_id"]
        platform = row["platform"].upper()
        account_id = str(row["account_id"])
        accounts.setdefault(client_id, {})[platform] = account_id
    return accounts


def get_event_config(config_rows: list[dict], client_id: str, platform: str, event_name: str) -> dict:
    """Returns {severity, goback_days, threshold} for an event, with defaults."""
    for row in config_rows:
        if (row["client_id"] == client_id
                and row["platform"].upper() == platform.upper()
                and row["event_name"] == event_name):
            return {
                "severity": row.get("severity", "secondary") or "secondary",
                "goback_days": int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS,
                "threshold": parse_threshold(row.get("baseline_threshold_pct")),
            }
    return {
        "severity": "secondary",
        "goback_days": DEFAULT_GOBACK_DAYS,
        "threshold": parse_threshold(None),
    }


def get_24h_events(config_rows: list[dict], client_id: str, platform: str) -> set:
    """Returns the set of event names flagged 24hBackGA4_48hBackGAds='sim' for a platform."""
    flagged = set()
    for row in config_rows:
        if (row["client_id"] == client_id and row["platform"].upper() == platform.upper()
                and str(row.get("24hBackGA4_48hBackGAds", "")).strip().lower() in TRUTHY):
            flagged.add(row["event_name"])
    return flagged


def _fetch_daily(account_id: str, platform: str, credentials: Credentials,
                 gads_client=None) -> dict[str, dict[str, float]]:
    if platform == "GA4":
        return fetch_daily_event_counts(account_id, credentials, days=BASELINE_DAYS)
    return fetch_daily_conversion_counts(account_id, gads_client, days=BASELINE_DAYS)


def _row(client_id: str, platform: str, event_name: str, severity: str,
         window: str, count: float, status: str, expected: float = None) -> dict:
    return {
        "client_id": client_id,
        "platform": platform,
        "event_name": event_name,
        "severity": severity,
        "window": window,
        "count": int(round(count)),
        "status": status,
        "expected": round(expected, 1) if expected is not None else "",
    }


def _check_platform(client_id: str, platform: str, display_name: str, account_id: str,
                    credentials: Credentials, config_rows: list[dict],
                    gads_client=None) -> list[dict]:
    """Runs all checks for one platform from a single 90-day daily fetch.

    The wide goback window, the short 24h/48h check and the weekday-median
    baseline are all computed locally from the same {event: {date: count}}
    matrix — one API call per (client, platform).
    """
    daily = _fetch_daily(account_id, platform, credentials, gads_client=gads_client)

    configured = {
        row["event_name"] for row in config_rows
        if row["client_id"] == client_id and row["platform"].upper() == platform
    }
    # Union with configured events so a fully dead event still produces a FAIL
    # row instead of silently vanishing from the API response.
    all_events = sorted(set(daily) | configured)

    yesterday = date.today() - timedelta(days=1)
    test_day = date.today() - timedelta(days=STABLE_DAY_OFFSET[platform])
    flagged = get_24h_events(config_rows, client_id, platform)

    results = []
    for event_name in all_events:
        cfg = get_event_config(config_rows, client_id, platform, event_name)
        day_map = daily.get(event_name, {})

        # --- Wide goback-window check (one row per event) ---
        goback_days = min(cfg["goback_days"], BASELINE_DAYS)
        wide_count = window_count(day_map, yesterday, goback_days)
        results.append(_row(
            client_id, display_name, event_name, cfg["severity"],
            f"{goback_days}d", wide_count, "OK" if wide_count > 0 else "FAIL",
        ))

        # --- Short check: automatic baseline above the volume floor, plain
        # zero-check for events explicitly flagged 24hBackGA4_48hBackGAds=sim ---
        expected = weekday_median(day_map, test_day)
        if expected >= BASELINE_MIN_MEDIAN:
            count = day_map.get(test_day.isoformat(), 0)
            status = short_check_status(count, expected, cfg["threshold"])
            results.append(_row(
                client_id, display_name, event_name, cfg["severity"],
                WINDOW_24H_LABEL[platform], count, status, expected=expected,
            ))
        elif event_name in flagged:
            count = window_count(day_map, yesterday, STABLE_DAY_OFFSET[platform])
            results.append(_row(
                client_id, display_name, event_name, cfg["severity"],
                WINDOW_24H_LABEL[platform], count, "OK" if count > 0 else "FAIL",
            ))

    return results


def run_checks(client_id: str, platforms: dict[str, str], credentials: Credentials,
               config_rows: list[dict]) -> list[dict]:
    results = []

    if "GA4" in platforms:
        results.extend(_check_platform(
            client_id, "GA4", "GA4", platforms["GA4"], credentials, config_rows
        ))

    if "GADS" in platforms:
        gads_client = get_gads_client(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        )
        results.extend(_check_platform(
            client_id, "GADS", "GAds", platforms["GADS"], credentials, config_rows,
            gads_client=gads_client
        ))

    return results


def main() -> None:
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")

    credentials = build_credentials()
    sheets_client = get_sheets_client(credentials)
    config_rows = read_config(sheet_id, sheets_client)
    client_accounts = build_client_accounts(config_rows)

    all_results = []
    for client_id, platforms in client_accounts.items():
        print(f"Checking {client_id}...")
        all_results.extend(run_checks(client_id, platforms, credentials, config_rows))

    write_results(sheet_id, sheets_client, all_results)

    critical_issues = [
        r for r in all_results
        if r["status"] in ("FAIL", "WARN") and r["severity"] == "critical"
    ]

    if critical_issues and slack_webhook:
        send_alert(slack_webhook, critical_issues)
        print(f"Slack alert sent: {len(critical_issues)} critical issue(s).")
    elif critical_issues:
        print(f"WARNING: {len(critical_issues)} critical issue(s) found but no Slack webhook configured.")
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()
