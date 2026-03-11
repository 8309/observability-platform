import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.span import format_span_id, format_trace_id

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "demo-api")
SERVICE_VERSION = os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
OTLP_BASE_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_BASE_ENDPOINT", "http://localhost:4318").rstrip("/")
TRACE_SAMPLE_RATIO = float(os.getenv("TRACE_SAMPLE_RATIO", "1.0"))
DEFAULT_SLOW_MIN_MS = int(os.getenv("SLOW_MIN_MS", "150"))
DEFAULT_SLOW_MAX_MS = int(os.getenv("SLOW_MAX_MS", "1200"))
DEFAULT_SLOW_FAIL_RATE = float(os.getenv("SLOW_FAIL_RATE", "0.25"))
HISTOGRAM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)

APP_LOGGER = logging.getLogger("demo-api")
TRACER = None
REQUESTS_TOTAL = None
REQUEST_DURATION_SECONDS = None
INFLIGHT_REQUESTS = None
TRACE_PROVIDER = None
METER_PROVIDER = None
LOGGER_PROVIDER = None
LOGGING_INSTRUMENTED = False


def _sanitize_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def _log_level() -> int:
    return getattr(logging, LOG_LEVEL, logging.INFO)


def _span_context_from_request(request: Request) -> Any:
    span = request.scope.get("otel_server_span")
    if span is not None:
        return span.get_span_context()

    current_span = trace.get_current_span()
    if current_span is not None:
        return current_span.get_span_context()

    return None


def _trace_identifiers(request: Request | None = None) -> tuple[str | None, str | None]:
    span_context = _span_context_from_request(request) if request is not None else trace.get_current_span().get_span_context()
    if span_context is None or not span_context.is_valid:
        return None, None
    return format_trace_id(span_context.trace_id), format_span_id(span_context.span_id)


class JsonFormatter(logging.Formatter):
    def _resolve_trace_id(self, record: logging.LogRecord) -> str | None:
        candidates = [
            getattr(record, "trace_id", None),
            getattr(record, "otelTraceID", None),
        ]
        for candidate in candidates:
            if candidate and candidate != "0" * 32:
                return str(candidate)

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            return format_trace_id(span_context.trace_id)
        return None

    def _resolve_span_id(self, record: logging.LogRecord) -> str | None:
        candidates = [
            getattr(record, "span_id", None),
            getattr(record, "otelSpanID", None),
        ]
        for candidate in candidates:
            if candidate and candidate != "0" * 16:
                return str(candidate)

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            return format_span_id(span_context.span_id)
        return None

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": SERVICE_NAME,
            "route": getattr(record, "route", None),
            "method": getattr(record, "method", None),
            "status_code": getattr(record, "status_code", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "trace_id": self._resolve_trace_id(record),
            "span_id": self._resolve_span_id(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        filtered_payload = {
            key: value
            for key, value in payload.items()
            if value is not None or key in {"trace_id", "span_id"}
        }
        return json.dumps(filtered_payload, separators=(",", ":"))


def _configure_logging(logger_provider: LoggerProvider) -> None:
    global LOGGING_INSTRUMENTED

    formatter = JsonFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(_log_level())
    stream_handler.setFormatter(formatter)

    otlp_handler = LoggingHandler(level=_log_level(), logger_provider=logger_provider)
    otlp_handler.setFormatter(formatter)

    for logger_name in ("demo-api", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(_log_level())
        logger.addHandler(stream_handler)
        logger.addHandler(otlp_handler)

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    if not LOGGING_INSTRUMENTED:
        LoggingInstrumentor().instrument(set_logging_format=False)
        LOGGING_INSTRUMENTED = True


def _configure_observability() -> None:
    global INFLIGHT_REQUESTS
    global LOGGER_PROVIDER
    global METER_PROVIDER
    global REQUESTS_TOTAL
    global REQUEST_DURATION_SECONDS
    global TRACE_PROVIDER
    global TRACER

    if TRACER is not None:
        return

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment": "local-demo",
        }
    )

    trace_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(_sanitize_probability(TRACE_SAMPLE_RATIO))),
    )
    trace_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{OTLP_BASE_ENDPOINT}/v1/traces")
        )
    )
    trace.set_tracer_provider(trace_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{OTLP_BASE_ENDPOINT}/v1/metrics"),
        export_interval_millis=5000,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
        views=[
            View(
                instrument_name="request_duration_seconds",
                aggregation=ExplicitBucketHistogramAggregation(boundaries=list(HISTOGRAM_BUCKETS)),
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{OTLP_BASE_ENDPOINT}/v1/logs")
        )
    )
    set_logger_provider(logger_provider)

    _configure_logging(logger_provider)

    meter = metrics.get_meter(SERVICE_NAME)
    TRACER = trace.get_tracer(SERVICE_NAME)
    REQUESTS_TOTAL = meter.create_counter(
        name="requests_total",
        description="Total HTTP requests by route, method, and status code.",
    )
    REQUEST_DURATION_SECONDS = meter.create_histogram(
        name="request_duration_seconds",
        unit="s",
        description="HTTP request latency in seconds.",
    )
    INFLIGHT_REQUESTS = meter.create_up_down_counter(
        name="inflight_requests",
        description="Current number of in-flight HTTP requests.",
    )
    TRACE_PROVIDER = trace_provider
    METER_PROVIDER = meter_provider
    LOGGER_PROVIDER = logger_provider


def _shutdown_observability() -> None:
    if LOGGER_PROVIDER is not None:
        LOGGER_PROVIDER.force_flush()
        LOGGER_PROVIDER.shutdown()
    if METER_PROVIDER is not None:
        METER_PROVIDER.force_flush()
        METER_PROVIDER.shutdown()
    if TRACE_PROVIDER is not None:
        TRACE_PROVIDER.force_flush()
        TRACE_PROVIDER.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    FastAPIInstrumentor.uninstrument_app(app)
    _shutdown_observability()


_configure_observability()
app = FastAPI(title="Observability Platform Demo", version=SERVICE_VERSION, lifespan=lifespan)


def server_request_hook(span: Any, scope: dict[str, Any]) -> None:
    scope["otel_server_span"] = span


FastAPIInstrumentor.instrument_app(app, server_request_hook=server_request_hook)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    INFLIGHT_REQUESTS.add(1)
    response = None

    try:
        response = await call_next(request)
    except Exception as exc:
        trace_id, span_id = _trace_identifiers(request)
        status_code = exc.status_code if isinstance(exc, HTTPException) else 500
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        response = JSONResponse(
            status_code=status_code,
            content={
                "status": "error",
                "message": message,
                "trace_id": trace_id,
            },
        )
        if status_code >= 500:
            APP_LOGGER.exception(
                "request failed",
                extra={
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "route": request.url.path,
                    "method": request.method,
                    "status_code": status_code,
                },
            )

    duration_seconds = time.perf_counter() - start_time
    route = getattr(request.scope.get("route"), "path", request.url.path)
    status_code = response.status_code if response is not None else 500
    attributes = {
        "route": route,
        "method": request.method,
        "status": str(status_code),
    }

    REQUESTS_TOTAL.add(1, attributes)
    REQUEST_DURATION_SECONDS.record(duration_seconds, attributes)
    INFLIGHT_REQUESTS.add(-1)

    trace_id, span_id = _trace_identifiers(request)
    if trace_id is not None and response is not None:
        response.headers["X-Trace-Id"] = trace_id

    APP_LOGGER.info(
        "request completed",
        extra={
            "trace_id": trace_id,
            "span_id": span_id,
            "route": route,
            "method": request.method,
            "status_code": status_code,
            "duration_ms": round(duration_seconds * 1000, 2),
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    trace_id, _ = _trace_identifiers(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "message": exc.detail,
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id} if trace_id else {},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    trace_id, _ = _trace_identifiers(request)
    return JSONResponse(
        status_code=422,
        content={
            "status": "validation_error",
            "errors": exc.errors(),
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id} if trace_id else {},
    )


@app.get("/ok")
async def ok(request: Request):
    with TRACER.start_as_current_span("fake-db", kind=SpanKind.INTERNAL) as fake_db_span:
        fake_db_span.set_attribute("db.system", "demo-db")
        fake_db_span.set_attribute("db.operation", "healthcheck")
        await asyncio.sleep(0.02)

    trace_id, _ = _trace_identifiers(request)
    return {
        "status": "ok",
        "message": "service healthy",
        "trace_id": trace_id,
    }


@app.get("/slow")
async def slow(
    request: Request,
    min_ms: int | None = None,
    max_ms: int | None = None,
    fail_rate: float | None = None,
):
    effective_min_ms = DEFAULT_SLOW_MIN_MS if min_ms is None else min_ms
    effective_max_ms = DEFAULT_SLOW_MAX_MS if max_ms is None else max_ms
    effective_fail_rate = DEFAULT_SLOW_FAIL_RATE if fail_rate is None else fail_rate

    if effective_min_ms < 0 or effective_max_ms < 0:
        raise HTTPException(status_code=400, detail="min_ms and max_ms must be non-negative")
    if effective_min_ms > effective_max_ms:
        raise HTTPException(status_code=400, detail="min_ms must be less than or equal to max_ms")
    if not 0.0 <= effective_fail_rate <= 1.0:
        raise HTTPException(status_code=400, detail="fail_rate must be between 0.0 and 1.0")

    total_delay_ms = random.randint(effective_min_ms, effective_max_ms)
    fake_db_delay_ms = max(10, int(total_delay_ms * random.uniform(0.3, 0.6)))
    external_delay_ms = max(5, total_delay_ms - fake_db_delay_ms)

    server_span = trace.get_current_span()
    server_span.set_attribute("app.slow.total_delay_ms", total_delay_ms)
    server_span.set_attribute("app.slow.fail_rate", effective_fail_rate)

    with TRACER.start_as_current_span("fake-db", kind=SpanKind.INTERNAL) as fake_db_span:
        fake_db_span.set_attribute("db.system", "demo-db")
        fake_db_span.set_attribute("db.operation", "select")
        fake_db_span.set_attribute("app.fake_db.delay_ms", fake_db_delay_ms)
        await asyncio.sleep(fake_db_delay_ms / 1000)

    with TRACER.start_as_current_span("external-call", kind=SpanKind.CLIENT) as external_call_span:
        external_call_span.set_attribute("http.method", "GET")
        external_call_span.set_attribute("server.address", "example.invalid")
        external_call_span.set_attribute("url.full", "https://example.invalid/demo")
        external_call_span.set_attribute("app.external_call.delay_ms", external_delay_ms)
        await asyncio.sleep(external_delay_ms / 1000)

    if random.random() < effective_fail_rate:
        error = RuntimeError("simulated downstream failure")
        server_span.record_exception(error)
        server_span.set_status(Status(StatusCode.ERROR, str(error)))
        raise error

    trace_id, _ = _trace_identifiers(request)
    return {
        "status": "ok",
        "delay_ms": total_delay_ms,
        "fail_rate": effective_fail_rate,
        "trace_id": trace_id,
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("APP_PORT", "8000")),
        access_log=False,
        log_config=None,
        reload=False,
    )
