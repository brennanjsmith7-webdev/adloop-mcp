from unittest.mock import Mock

import pytest
from google.analytics.data_v1beta.types import RunRealtimeReportResponse, RunReportResponse

from adloop.config import AdLoopConfig
from adloop.ga4 import reports


@pytest.mark.parametrize("property_id", ["482153682", "properties/482153682"])
@pytest.mark.parametrize(
    ("report", "method", "response_type"),
    [
        (reports.run_ga4_report, "run_report", RunReportResponse),
        (reports.run_realtime_report, "run_realtime_report", RunRealtimeReportResponse),
    ],
)
def test_reports_use_ga4_property_resource_name(
    monkeypatch, property_id, report, method, response_type
):
    client = Mock()
    endpoint = getattr(client, method)
    endpoint.return_value = response_type()
    monkeypatch.setattr("adloop.ga4.client.get_data_client", lambda config: client)

    result = report(AdLoopConfig(), property_id=property_id, metrics=["eventCount"])

    endpoint.assert_called_once()
    assert endpoint.call_args.args[0].property == "properties/482153682"
    assert result["property"] == "properties/482153682"
