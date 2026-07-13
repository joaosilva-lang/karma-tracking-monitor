from datetime import date, timedelta
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from google.oauth2.credentials import Credentials

from src.baseline import normalize_date


def get_ga4_client(credentials: Credentials) -> BetaAnalyticsDataClient:
    return BetaAnalyticsDataClient(credentials=credentials)


def fetch_event_counts(property_id: str, credentials: Credentials, days: int = 7) -> dict[str, int]:
    """Returns {event_name: count} for all events in the last `days` days."""
    client = get_ga4_client(credentials)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )],
    )

    response = client.run_report(request)

    return {
        row.dimension_values[0].value: int(row.metric_values[0].value)
        for row in response.rows
    }


def fetch_daily_event_data(property_id: str, credentials: Credentials,
                           days: int = 90) -> tuple[dict, dict]:
    """Returns (counts, values): two {event_name: {date_str: n}} maps over the
    last `days` days, from a single API call. `values` is the daily sum of the
    event's `value` parameter (GA4 metric eventValue)."""
    client = get_ga4_client(credentials)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="eventName"), Dimension(name="date")],
        metrics=[Metric(name="eventCount"), Metric(name="eventValue")],
        date_ranges=[DateRange(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )],
    )

    response = client.run_report(request)

    counts: dict[str, dict[str, int]] = {}
    values: dict[str, dict[str, float]] = {}
    for row in response.rows:
        event_name = row.dimension_values[0].value
        day = normalize_date(row.dimension_values[1].value)
        counts.setdefault(event_name, {})[day] = int(row.metric_values[0].value)
        values.setdefault(event_name, {})[day] = float(row.metric_values[1].value)
    return counts, values


def fetch_daily_event_counts(property_id: str, credentials: Credentials,
                             days: int = 90) -> dict[str, dict[str, int]]:
    """Returns {event_name: {date_str: count}} per day over the last `days` days."""
    counts, _ = fetch_daily_event_data(property_id, credentials, days=days)
    return counts
