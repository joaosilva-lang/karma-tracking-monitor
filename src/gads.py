from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


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

    query = f"""
        SELECT
            segments.conversion_action_name,
            metrics.all_conversions
        FROM customer
        WHERE segments.date DURING LAST_7_DAYS
    """

    results: dict[str, float] = {}

    try:
        response = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in response:
            for row in batch.results:
                name = row.segments.conversion_action_name
                count = row.metrics.all_conversions
                results[name] = results.get(name, 0.0) + count
    except GoogleAdsException as ex:
        raise RuntimeError(
            f"GAds API error for customer {customer_id}: {ex.error.code().name}"
        ) from ex

    return {name: int(count) for name, count in results.items()}
