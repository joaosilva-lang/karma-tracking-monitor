from datetime import date, timedelta
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    GetMetadataRequest,
    Metric,
    RunReportRequest,
)
from google.oauth2.credentials import Credentials

from src.baseline import normalize_date

# Params that map to a GA4 built-in dimension instead of a registered custom
# dimension. Getting an entry here wrong is not critical: fetch_registered_dimensions
# just won't find that name registered, and the param falls back to being
# treated as an (unregistered) custom dimension name.
BUILTIN_PARAM_DIMENSIONS = {
    "transaction_id": "transactionId",
    "currency": "currencyId",
    "item_id": "itemId",
    "item_name": "itemName",
    "search_term": "searchTerm",
    "page_location": "pageLocation",
    "link_url": "linkUrl",
    "file_name": "fileName",
    "method": "method",
}


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


def fetch_registered_dimensions(property_id: str, credentials: Credentials) -> set[str]:
    """Returns the api_name of every dimension registered on this GA4
    property — built-in (e.g. `transactionId`) and custom event-scoped ones
    (e.g. `customEvent:loyalty_tier`) alike — via a single metadata call.

    This is existence/registration only, never values or daily presence: one
    call per property, no date range, no per-parameter query. Cross-reference
    a param name against this set (via BUILTIN_PARAM_DIMENSIONS for the
    built-in form, or `customEvent:<param>` otherwise) to know whether GA4
    can report on it at all.
    """
    client = get_ga4_client(credentials)
    metadata = client.get_metadata(GetMetadataRequest(name=f"properties/{property_id}/metadata"))
    return {d.api_name for d in metadata.dimensions}
