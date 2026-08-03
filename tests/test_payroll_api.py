import json
import urllib.error
import urllib.request

import pytest

from payroll_api import (
    PayrollApiConfig,
    PayrollApiError,
    PayrollApiServer,
    build_payroll_payload,
)


def _settlement(period, *, sources, worker):
    assert period == "2026-07"
    assert sources == {"loaded": True}
    amount = 1200 if worker == "Кирилл" else 2300
    return {
        "period_label": "Июль 2026",
        "worker": worker,
        "status": "pending",
        "display_service_count": 2,
        "display_service_sum": amount - 300,
        "display_purchase_sum": 100,
        "display_travel_count": 1,
        "display_travel_sum": 100,
        "display_salary_task_count": 1,
        "display_salary_task_sum": 100,
        "correction": 0,
        "display_total": amount,
    }


def test_build_payroll_payload_uses_canonical_calculation_once_per_worker():
    payload = build_payroll_payload(
        {"version": "1", "period": "2026-07", "workers": ["кирилл", "Александр"]},
        paid_workers=["Кирилл", "Александр"],
        sources_factory=lambda: {"loaded": True},
        settlement_factory=_settlement,
    )

    assert payload["period_label"] == "Июль 2026"
    assert [row["worker"] for row in payload["workers"]] == [
        "Кирилл",
        "Александр",
    ]
    assert payload["total"] == 3500
    assert payload["workers"][0]["service_sum"] == 900


@pytest.mark.parametrize(
    "payload_request",
    [
        {"version": "1", "period": "07.2026"},
        {"version": "1", "period": "2026-07", "workers": ["Неизвестный"]},
        {"version": "2", "period": "2026-07"},
        {"version": "1", "period": "2026-07", "extra": True},
    ],
)
def test_build_payroll_payload_rejects_invalid_requests(payload_request):
    with pytest.raises(PayrollApiError):
        build_payroll_payload(
            payload_request,
            paid_workers=["Кирилл"],
            sources_factory=lambda: {"loaded": True},
            settlement_factory=_settlement,
        )


def test_http_server_requires_bearer_and_returns_payload():
    token = "t" * 32
    server = PayrollApiServer(
        PayrollApiConfig(token=token, host="127.0.0.1", port=0),
        payload_builder=lambda _request: {"version": "1", "available": True},
    )
    port = server._server.server_address[1]
    server.start()
    try:
        unauthenticated = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/owner-ai/payroll",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(unauthenticated, timeout=2)
        assert exc.value.code == 401

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/internal/owner-ai/payroll",
            data=json.dumps({"version": "1"}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.loads(response.read()) == {
                "version": "1",
                "available": True,
            }
    finally:
        server.close()
