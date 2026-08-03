"""Small authenticated read-only HTTP API for owner payroll summaries."""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence


logger = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"20\d{2}-(?:0[1-9]|1[0-2])\Z")
_MAX_REQUEST_BYTES = 8_192


class PayrollApiError(ValueError):
    """A caller supplied an invalid payroll query."""


@dataclass(frozen=True, slots=True)
class PayrollApiConfig:
    token: str
    port: int
    host: str = "0.0.0.0"

    def __post_init__(self) -> None:
        if len(self.token.strip()) < 32:
            raise ValueError("Payroll API token must contain at least 32 characters.")
        # Port 0 is useful for an OS-assigned ephemeral port in tests.
        if not 0 <= self.port <= 65_535:
            raise ValueError("Payroll API port is invalid.")


def build_payroll_payload(
    request: Mapping[str, Any],
    *,
    paid_workers: Sequence[str],
    sources_factory: Callable[[], Mapping[str, Any]],
    settlement_factory: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a bounded snapshot using the bot's canonical payout calculation."""
    if set(request) - {"version", "period", "workers"}:
        raise PayrollApiError("Unexpected request fields.")
    if request.get("version") != "1":
        raise PayrollApiError("Unsupported request version.")
    period = request.get("period")
    if not isinstance(period, str) or _PERIOD_RE.fullmatch(period) is None:
        raise PayrollApiError("period must use YYYY-MM.")

    canonical_workers = {
        " ".join(str(worker).split()).casefold(): " ".join(str(worker).split())
        for worker in paid_workers
        if " ".join(str(worker).split())
    }
    raw_workers = request.get("workers")
    if raw_workers is None:
        workers = list(canonical_workers.values())
    elif (
        isinstance(raw_workers, Sequence)
        and not isinstance(raw_workers, (str, bytes, bytearray))
        and 1 <= len(raw_workers) <= 8
    ):
        workers = []
        for raw_worker in raw_workers:
            if not isinstance(raw_worker, str):
                raise PayrollApiError("workers must contain text names.")
            key = " ".join(raw_worker.split()).casefold()
            worker = canonical_workers.get(key)
            if worker is None:
                raise PayrollApiError("Unknown payroll worker.")
            if worker not in workers:
                workers.append(worker)
    else:
        raise PayrollApiError("workers must be a non-empty bounded list.")
    if not workers:
        raise PayrollApiError("No paid workers are configured.")

    # The HTTP contract uses the conventional YYYY-MM form, while the
    # long-standing bot settlement engine stores month keys as MM.YYYY.
    # Keep that translation at this boundary so callers never need to know
    # about the internal spreadsheet key format.
    year, month = period.split("-", 1)
    settlement_period = f"{month}.{year}"

    sources = sources_factory()
    rows: list[dict[str, Any]] = []
    total = 0.0
    period_label = period
    for worker in workers:
        settlement = settlement_factory(
            settlement_period,
            sources=sources,
            worker=worker,
        )
        period_label = str(settlement.get("period_label") or period)[:80]
        display_total = _number(settlement.get("display_total"))
        total += display_total
        rows.append(
            {
                "worker": worker,
                "status": str(settlement.get("status") or "pending")[:40],
                "has_snapshot": settlement.get("has_snapshot") is True,
                "has_post_close_changes": (
                    settlement.get("has_post_close_changes") is True
                ),
                "service_count": _integer(
                    settlement.get("display_service_count")
                ),
                "service_sum": _number(
                    settlement.get("display_service_sum")
                ),
                "purchase_sum": _number(
                    settlement.get("display_purchase_sum")
                ),
                "travel_count": _integer(
                    settlement.get("display_travel_count")
                ),
                "travel_sum": _number(
                    settlement.get("display_travel_sum")
                ),
                "salary_task_count": _integer(
                    settlement.get("display_salary_task_count")
                ),
                "salary_task_sum": _number(
                    settlement.get("display_salary_task_sum")
                ),
                "correction": _number(settlement.get("correction")),
                "total": display_total,
            }
        )
    return {
        "version": "1",
        "available": True,
        "period": period,
        "period_label": period_label,
        "workers": rows,
        "total": round(total, 2),
        "currency": "RUB",
    }


class PayrollApiServer:
    """Background server exposing exactly one authenticated read operation."""

    def __init__(
        self,
        config: PayrollApiConfig,
        *,
        payload_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self.config = config
        self.payload_builder = payload_builder
        self._lock = threading.Lock()
        handler = self._handler_type()
        self._server = ThreadingHTTPServer((config.host, config.port), handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="owner-payroll-api",
            daemon=True,
        )
        self._thread.start()
        logger.info("Owner payroll API is listening on port %s", self.config.port)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CoffeePayroll/1"

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._json(200, {"status": "ok"})
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/internal/owner-ai/payroll":
                    self._json(404, {"error": "not_found"})
                    return
                authorization = self.headers.get("Authorization", "")
                expected = f"Bearer {owner.config.token}"
                if not hmac.compare_digest(authorization, expected):
                    self._json(401, {"error": "unauthorized"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(400, {"error": "invalid_request"})
                    return
                if not 1 <= size <= _MAX_REQUEST_BYTES:
                    self._json(413, {"error": "request_too_large"})
                    return
                try:
                    raw = self.rfile.read(size)
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, Mapping):
                        raise PayrollApiError("Request must be an object.")
                    with owner._lock:
                        response = owner.payload_builder(request)
                except (PayrollApiError, UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid_request"})
                    return
                except Exception:
                    logger.exception("Owner payroll API request failed")
                    self._json(503, {"error": "temporarily_unavailable"})
                    return
                self._json(200, response)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _json(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(
                    dict(payload), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0
