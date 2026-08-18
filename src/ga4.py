from datetime import date, timedelta
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    RunReportRequest,
)
from google.api_core.exceptions import InvalidArgument
from google.oauth2.credentials import Credentials

from src.baseline import normalize_date

# Params that map to a GA4 built-in dimension instead of a registered custom
# dimension. Getting an entry here wrong is not critical: the wrong dimension
# name simply errors out and fetch_daily_param_presence falls back to trying
# `customEvent:<param>` instead.
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


def _param_dimension_candidates(param: str) -> list[str]:
    """Dimension name(s) to try for an event parameter, in resolution order.

    A parameter is either a GA4 built-in dimension or, for anything else, a
    registered event-scoped custom dimension (`customEvent:<param>`). Both
    forms are tried when `param` is in BUILTIN_PARAM_DIMENSIONS, so a wrong or
    missing map entry degrades to the custom-dimension form instead of hard
    failing.
    """
    custom = f"customEvent:{param}"
    builtin = BUILTIN_PARAM_DIMENSIONS.get(param)
    return [builtin, custom] if builtin else [custom]


def fetch_daily_param_presence(property_id: str, credentials: Credentials, param: str,
                               days: int = 90, event_names: list[str] = None) -> dict | None:
    """Returns {event_name: {date_str: n}} of daily counts where `param` was
    actually present on the hit (not the literal "(not set)"), or None if the
    parameter is invisible to the API — not a built-in dimension and not
    registered as a custom dimension. Absence is discovered by trying the
    query and catching the resulting error; there is no other way to ask GA4
    "does this dimension exist" up front.

    The parameter's dimension is used ONLY in the filter, never added to the
    response dimensions. Adding it would make GA4 return one row per distinct
    value (e.g. one per transaction_id); for a busy event that fans the
    response out past the API's ~10k row cap, which truncates silently and
    would undercount presence into a false FAIL. Filtering costs nothing
    extra — GA4 still evaluates the dimension, it just doesn't split rows on it.

    `event_names`, when given, adds an `eventName IN (...)` filter to scope the
    query to only the events that need this parameter (used by the daily
    check, which only verifies critical events).

    Same return shape as fetch_daily_event_data's `counts` — {event_name:
    {date: count}} — so every helper in src/baseline.py (window_count,
    is_value_carrying, weekday_median, ...) works on it unmodified.
    """
    client = get_ga4_client(credentials)
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    event_filter = None
    if event_names:
        event_filter = FilterExpression(filter=Filter(
            field_name="eventName",
            in_list_filter=Filter.InListFilter(values=list(event_names)),
        ))

    for dimension_name in _param_dimension_candidates(param):
        not_set_filter = FilterExpression(filter=Filter(
            field_name=dimension_name,
            string_filter=Filter.StringFilter(
                value="(not set)",
                match_type=Filter.StringFilter.MatchType.EXACT,
            ),
        ))
        present_filter = FilterExpression(not_expression=not_set_filter)
        dimension_filter = (
            FilterExpression(and_group=FilterExpressionList(
                expressions=[present_filter, event_filter]))
            if event_filter else present_filter
        )

        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name="eventName"), Dimension(name="date")],
            metrics=[Metric(name="eventCount")],
            date_ranges=[DateRange(
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )],
            dimension_filter=dimension_filter,
        )
        try:
            response = client.run_report(request)
        except InvalidArgument:
            # This dimension name doesn't exist on this property — try the
            # next form (built-in name failed -> fall back to customEvent:).
            continue

        presence: dict[str, dict[str, int]] = {}
        for row in response.rows:
            event_name = row.dimension_values[0].value
            day = normalize_date(row.dimension_values[1].value)
            presence.setdefault(event_name, {})[day] = int(row.metric_values[0].value)
        return presence

    return None
