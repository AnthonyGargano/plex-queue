"""Optional OpenTelemetry wiring (OTLP → local collector → Kafka / Grafana).

Fail-open: missing packages, disabled config, or exporter errors must not
stop the Telegram bot. Endpoint is restricted to loopback.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from contextlib import contextmanager
from typing import Any, Iterator

from plex_queue.errors import public_error
from plex_queue.sensitive import is_sensitive_key

log = logging.getLogger(__name__)

_active = False
_tracer = None
_event_counter = None
_import_counter = None
_tidy_counter = None
_duration_hist = None
_providers: list[Any] = []
_log_handler: logging.Handler | None = None


def setup_otel(
    *,
    enabled: bool,
    endpoint: str,
    service_name: str,
    service_version: str = "0.1.0",
) -> bool:
    """Initialize OTEL providers. Returns True when export is active."""
    global _active, _tracer, _event_counter, _import_counter, _tidy_counter
    global _duration_hist, _providers, _log_handler

    shutdown_otel()
    if not enabled:
        return False
    try:
        endpoint = validate_otel_endpoint(endpoint)
    except ValueError as exc:
        log.warning("otel disabled: %s", exc)
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning(
            "otel.enabled but OpenTelemetry packages missing; "
            'install with: pip install -e ".[otel]"'
        )
        return False

    created: list[Any] = []
    handler: logging.Handler | None = None
    try:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        created.append(tracer_provider)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True),
            export_interval_millis=15000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        created.append(meter_provider)
        metrics.set_meter_provider(meter_provider)

        logger_provider = LoggerProvider(resource=resource)
        created.append(logger_provider)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
        )
        set_logger_provider(logger_provider)

        # Bridge plex_queue.events JSON lines into the OTEL logs pipeline.
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        events_log = logging.getLogger("plex_queue.events")
        events_log.addHandler(handler)

        meter = metrics.get_meter("plex_queue")
        _tracer = trace.get_tracer("plex_queue")
        _event_counter = meter.create_counter(
            "plex_queue.events",
            description="Domain events emitted by plex-queue",
        )
        _import_counter = meter.create_counter(
            "plex_queue.import.cycles",
            description="Import cycle completions",
        )
        _tidy_counter = meter.create_counter(
            "plex_queue.tidy.applies",
            description="Tidy apply outcomes",
        )
        _duration_hist = meter.create_histogram(
            "plex_queue.operation.duration",
            unit="ms",
            description="Operation duration in milliseconds",
        )
        _providers = list(created)
        _log_handler = handler
        _active = True
        log.info("otel export enabled → %s", endpoint)
        return True
    except Exception as exc:  # fail-open
        log.warning("otel setup failed; continuing without export: %s", exc)
        if handler is not None:
            logging.getLogger("plex_queue.events").removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        for provider in created:
            try:
                provider.shutdown()
            except Exception:
                pass
        _active = False
        _tracer = None
        _event_counter = None
        _import_counter = None
        _tidy_counter = None
        _duration_hist = None
        _providers = []
        _log_handler = None
        return False


def shutdown_otel() -> None:
    global _active, _tracer, _event_counter, _import_counter, _tidy_counter
    global _duration_hist, _providers, _log_handler

    if _log_handler is not None:
        logging.getLogger("plex_queue.events").removeHandler(_log_handler)
        try:
            _log_handler.close()
        except Exception:
            pass
        _log_handler = None

    providers = list(_providers)
    _active = False
    _tracer = None
    _event_counter = None
    _import_counter = None
    _tidy_counter = None
    _duration_hist = None
    _providers = []
    for provider in providers:
        try:
            provider.shutdown()
        except Exception as exc:
            log.debug("otel provider shutdown: %s", exc)


def is_active() -> bool:
    return _active


def validate_otel_endpoint(endpoint: str) -> str:
    """Require http(s) to loopback only (no remote telemetry exfil via config)."""
    text = str(endpoint or "").strip()
    if not text:
        raise ValueError("otel.endpoint is empty")
    parsed = urllib.parse.urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("otel.endpoint must use http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("otel.endpoint must not include credentials")
    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "otel.endpoint host must be loopback (127.0.0.1, localhost, or ::1)"
        )
    if host == "localhost":
        _assert_localhost_loopback()
    if parsed.port is None:
        # Bracket IPv6 literals so clients see http://[::1]:4317
        host_part = f"[{host}]" if ":" in host else host
        text = f"{scheme}://{host_part}:4317"
    return text


def _assert_localhost_loopback() -> None:
    """Refuse localhost if DNS/hosts maps it off loopback."""
    try:
        answers = socket.getaddrinfo("localhost", None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("otel.endpoint localhost could not be resolved") from exc
    if not answers:
        raise ValueError("otel.endpoint localhost could not be resolved")
    seen_ip = False
    for entry in answers:
        address = entry[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        seen_ip = True
        if not ip.is_loopback:
            raise ValueError(
                f"otel.endpoint localhost resolves to non-loopback address: {address}"
            )
    if not seen_ip:
        raise ValueError("otel.endpoint localhost did not resolve to an IP address")


def record_event(event: str, fields: dict[str, Any]) -> None:
    """Record metrics and a span event for a domain emit() payload."""
    if not _active:
        return
    status = str(fields.get("status") or "")
    attrs = _otel_attrs(event, fields)
    try:
        if _event_counter is not None:
            _event_counter.add(1, {"event": event, "status": status or "unknown"})
        if event == "import.cycle" and _import_counter is not None:
            _import_counter.add(1, {"status": status or "unknown"})
        if event in {"tidy.apply", "tidy.apply_batch"} and _tidy_counter is not None:
            _tidy_counter.add(1, {"status": status or "unknown"})
        duration = fields.get("duration_ms")
        if isinstance(duration, (int, float)) and _duration_hist is not None:
            _duration_hist.record(
                float(duration),
                {"operation": event, "status": status or "unknown"},
            )
        from opentelemetry import trace

        current = trace.get_current_span()
        if current is not None and current.is_recording():
            current.add_event(event, attributes=attrs)
            if status in {"fail", "failed", "error"}:
                from opentelemetry.trace import Status, StatusCode

                current.set_status(Status(StatusCode.ERROR, status))
    except Exception as exc:
        log.debug("otel record_event failed: %s", exc)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a trace span when OTEL is active; otherwise a no-op."""
    if not _active or _tracer is None:
        yield None
        return
    attrs = {
        key: value
        for key, value in attributes.items()
        if value is not None
        and isinstance(value, (str, int, float, bool))
        and not is_sensitive_key(key)
    }
    with _tracer.start_as_current_span(
        name,
        attributes=attrs,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as exc:
            from opentelemetry.trace import Status, StatusCode

            # Manual status only — avoid exporting raw exception message/stack.
            current.set_status(Status(StatusCode.ERROR, public_error(exc)))
            raise


def _otel_attrs(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if key in {"event", "ts", "service"} or value is None:
            continue
        if is_sensitive_key(key):
            continue
        if isinstance(value, (str, int, float, bool)):
            attrs[key] = value
        elif isinstance(value, (list, tuple)):
            attrs[key] = ",".join(str(item) for item in value[:20])
        elif isinstance(value, dict):
            # Refuse nested objects in OTEL attrs (could hide secret keys).
            continue
        else:
            attrs[key] = str(value)[:200]
    return attrs
