"""GA4 report tools — account summaries and custom reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adloop.config import normalize_ga4_property_id

if TYPE_CHECKING:
    from adloop.config import AdLoopConfig


def get_account_summaries(config: AdLoopConfig) -> dict:
    """List GA4 accounts and properties accessible by the authenticated user."""
    from adloop.ga4.client import get_admin_client

    client = get_admin_client(config)
    summaries = client.list_account_summaries()

    accounts = []
    for summary in summaries:
        properties = []
        for prop in summary.property_summaries:
            properties.append({
                "property": prop.property,
                "display_name": prop.display_name,
            })
        accounts.append({
            "account": summary.account,
            "display_name": summary.display_name,
            "properties": properties,
        })

    return {
        "accounts": accounts,
        "total_accounts": len(accounts),
        "total_properties": sum(len(a["properties"]) for a in accounts),
    }


def run_ga4_report(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    date_range_start: str = "7daysAgo",
    date_range_end: str = "today",
    limit: int = 100,
) -> dict:
    """Run a GA4 report with specified dimensions, metrics, and date range."""
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    from adloop.ga4.client import get_data_client

    if not dimensions and not metrics:
        return {"error": "At least one dimension or metric must be specified."}

    property_id = normalize_ga4_property_id(property_id)
    client = get_data_client(config)

    request = RunReportRequest(
        property=property_id,
        dimensions=[Dimension(name=d) for d in (dimensions or [])],
        metrics=[Metric(name=m) for m in (metrics or [])],
        date_ranges=[DateRange(start_date=date_range_start, end_date=date_range_end)],
        limit=limit,
    )

    response = client.run_report(request)

    dim_headers = [h.name for h in response.dimension_headers]
    met_headers = [h.name for h in response.metric_headers]

    rows = []
    for row in response.rows:
        r = {}
        for i, val in enumerate(row.dimension_values):
            r[dim_headers[i]] = val.value
        for i, val in enumerate(row.metric_values):
            r[met_headers[i]] = val.value
        rows.append(r)

    return {
        "property": property_id,
        "date_range": {"start": date_range_start, "end": date_range_end},
        "dimensions": dim_headers,
        "metrics": met_headers,
        "rows": rows,
        "row_count": len(rows),
        "total_row_count": response.row_count,
    }


def run_realtime_report(
    config: AdLoopConfig,
    *,
    property_id: str = "",
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
) -> dict:
    """Run a GA4 realtime report."""
    from google.analytics.data_v1beta.types import (
        Dimension,
        Metric,
        RunRealtimeReportRequest,
    )

    from adloop.ga4.client import get_data_client

    property_id = normalize_ga4_property_id(property_id)
    client = get_data_client(config)

    request = RunRealtimeReportRequest(
        property=property_id,
        dimensions=[Dimension(name=d) for d in (dimensions or [])],
        metrics=[Metric(name=m) for m in (metrics or ["activeUsers"])],
    )

    response = client.run_realtime_report(request)

    dim_headers = [h.name for h in response.dimension_headers]
    met_headers = [h.name for h in response.metric_headers]

    rows = []
    for row in response.rows:
        r = {}
        for i, val in enumerate(row.dimension_values):
            r[dim_headers[i]] = val.value
        for i, val in enumerate(row.metric_values):
            r[met_headers[i]] = val.value
        rows.append(r)

    return {
        "property": property_id,
        "dimensions": dim_headers,
        "metrics": met_headers,
        "rows": rows,
        "row_count": len(rows),
    }
