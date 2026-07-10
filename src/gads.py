from datetime import date, timedelta
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from src.gtm import extract_conversion_label


def get_gads_client(client_id: str, client_secret: str, refresh_token: str,
                    developer_token: str, login_customer_id: str) -> GoogleAdsClient:
    credentials = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "developer_token": developer_token,
        "login_customer_id": login_customer_id,
        "use_proto_plus": True,
    }
    return GoogleAdsClient.load_from_dict(credentials)


def fetch_conversion_counts(customer_id: str, client: GoogleAdsClient, days: int = 7) -> dict[str, int]:
    """Returns {conversion_action_name: count} for the last `days` days."""
    ga_service = client.get_service("GoogleAdsService")

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    query = f"""
        SELECT
            conversion_action.name,
            metrics.all_conversions
        FROM conversion_action
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND conversion_action.status = 'ENABLED'
    """

    results: dict[str, float] = {}

    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            name = row.conversion_action.name
            count = row.metrics.all_conversions
            results[name] = results.get(name, 0.0) + count
    except GoogleAdsException as ex:
        raise RuntimeError(
            f"GAds API error for customer {customer_id}: {ex.error.code().name}"
        ) from ex

    return {name: int(count) for name, count in results.items()}


def fetch_daily_conversion_counts(customer_id: str, client: GoogleAdsClient,
                                  days: int = 90) -> dict[str, dict[str, float]]:
    """Returns {conversion_action_name: {date_str: count}} per day over the last `days` days."""
    ga_service = client.get_service("GoogleAdsService")

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    query = f"""
        SELECT
            conversion_action.name,
            segments.date,
            metrics.all_conversions
        FROM conversion_action
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND conversion_action.status = 'ENABLED'
    """

    result: dict[str, dict[str, float]] = {}

    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            name = row.conversion_action.name
            day = row.segments.date
            count = row.metrics.all_conversions
            day_map = result.setdefault(name, {})
            day_map[day] = day_map.get(day, 0.0) + count
    except GoogleAdsException as ex:
        raise RuntimeError(
            f"GAds API error for customer {customer_id}: {ex.error.code().name}"
        ) from ex

    return result


def fetch_conversion_labels(customer_id: str, client: GoogleAdsClient) -> dict[str, str]:
    """Returns {conversion_action_name: conversion_label} for enabled actions.

    The label (the part after the slash in send_to 'AW-XXXX/label') lives in
    the action's tag snippets; it's what GTM `awct` tags reference, so it is
    the join key between GAds conversion actions and GTM tags.
    """
    ga_service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            conversion_action.name,
            conversion_action.tag_snippets
        FROM conversion_action
        WHERE conversion_action.status = 'ENABLED'
    """

    labels: dict[str, str] = {}

    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        for row in response:
            for snippet in row.conversion_action.tag_snippets:
                label = extract_conversion_label(snippet.event_snippet)
                if label:
                    labels[row.conversion_action.name] = label
                    break
    except GoogleAdsException as ex:
        raise RuntimeError(
            f"GAds API error for customer {customer_id}: {ex.error.code().name}"
        ) from ex

    return labels
