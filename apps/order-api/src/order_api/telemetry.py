"""Logging, metrics and tracing setup.

- Logs: one JSON object per line on stdout, with the trace id when there is one (Loki-friendly).
- Metrics: Prometheus /metrics (request count, latency histogram, by status), used later by the
  canary analysis and SLOs.
- Traces: OpenTelemetry, exported over OTLP only when OTEL_EXPORTER_OTLP_ENDPOINT is set, so the
  service runs fine without a collector.
"""

import json
import logging
import os
import sys

from fastapi import FastAPI
from opentelemetry import trace
from prometheus_client import CollectorRegistry, gc_collector, platform_collector, process_collector
from prometheus_fastapi_instrumentator import Instrumentator


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str) -> None:
        super().__init__()
        self._service, self._version = service, version

    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span().get_span_context()
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "service": self._service,
            "version": self._version,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(service: str, version: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, version))
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)


def setup_metrics(app: FastAPI) -> None:
    # One registry per app instead of the process-global default: no cross-talk between app
    # instances (matters in tests) and an explicit list of what this service exports.
    registry = CollectorRegistry()
    process_collector.ProcessCollector(registry=registry)
    platform_collector.PlatformCollector(registry=registry)
    gc_collector.GCCollector(registry=registry)
    Instrumentator(
        excluded_handlers=["/healthz", "/readyz", "/metrics"], registry=registry
    ).instrument(app).expose(app, include_in_schema=False)


def setup_tracing(app: FastAPI, service: str, version: str) -> None:
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "service.version": version})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    HTTPXClientInstrumentor().instrument()
