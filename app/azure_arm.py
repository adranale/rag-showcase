"""Minimal Azure Resource Manager client using the Container Apps managed identity."""

import os
from datetime import UTC, datetime, timedelta

import httpx

ARM = "https://management.azure.com"
SERVER_API_VERSION = "2024-08-01"
METRICS_API_VERSION = "2018-01-01"


def _token() -> str:
    endpoint = os.getenv("IDENTITY_ENDPOINT")
    header = os.getenv("IDENTITY_HEADER")
    if not endpoint or not header:
        raise RuntimeError("Managed identity is not enabled for this app")

    response = httpx.get(
        endpoint,
        params={"resource": ARM + "/", "api-version": "2019-08-01"},
        headers={"X-IDENTITY-HEADER": header},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _call(method: str, resource_id: str, suffix: str, params: dict) -> httpx.Response:
    response = httpx.request(
        method,
        f"{ARM}{resource_id}{suffix}",
        params=params,
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    response.raise_for_status()
    return response


def server_state(server_id: str) -> str:
    body = _call("GET", server_id, "", {"api-version": SERVER_API_VERSION}).json()
    return body["properties"]["state"]


def start_server(server_id: str) -> None:
    _call("POST", server_id, "/start", {"api-version": SERVER_API_VERSION})


def stop_server(server_id: str) -> None:
    _call("POST", server_id, "/stop", {"api-version": SERVER_API_VERSION})


def request_count(app_id: str, minutes: int) -> int:
    """Total ingress requests of a Container App during the last `minutes`."""
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    body = _call(
        "GET",
        app_id,
        "/providers/Microsoft.Insights/metrics",
        {
            "api-version": METRICS_API_VERSION,
            "metricnames": "Requests",
            "aggregation": "Total",
            "interval": "PT1M",
            "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        },
    ).json()

    total = 0
    for metric in body.get("value", []):
        for series in metric.get("timeseries", []):
            total += sum(point.get("total") or 0 for point in series.get("data", []))
    return int(total)
