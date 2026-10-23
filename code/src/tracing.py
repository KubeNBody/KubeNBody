# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""OpenTelemetry traces, exported over OTLP gRPC to the monitoring stack (Tempo).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (the manifests set it), and
OTEL_TRACING_ENABLED=false turns it off. Without a provider every span is a
no-op, so traced code runs the same with tracing off."""
import functools
import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


logger = logging.getLogger(__name__)

EXPORT_TIMEOUT_S = 2
# requests not worth a span: the probes and the metrics scrapes. The
# instrumentation searches each regex in the full URL (scheme://host/path),
# so each is anchored to the whole path: /api/comparison/jobs/1/metrics
# is traced.
UNTRACED_PATHS = ("/metrics", "/api/live", "/api/ready", "/api/health")
UNTRACED_URLS = ",".join(f"^[a-z]+://[^/]+{path}$" for path in UNTRACED_PATHS)

_initialized = False


def setup_tracing(service_name: str) -> bool:
    """Install the OTLP exporter once per process. True if tracing is on."""
    global _initialized
    if _initialized:
        return True
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint or os.getenv("OTEL_TRACING_ENABLED", "true").lower() == "false":
        return False
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})
    provider = TracerProvider(resource=resource)
    # A short export deadline: with the collector down (the cluster's LGTM is
    # scaled to zero by default) a batch is dropped in seconds and shutdown
    # does not hold up a rollout
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True, timeout=EXPORT_TIMEOUT_S)
    provider.add_span_processor(BatchSpanProcessor(exporter, export_timeout_millis=EXPORT_TIMEOUT_S * 1000))
    trace.set_tracer_provider(provider)
    _initialized = True
    logger.info(f"OpenTelemetry tracing on: service={service_name}, endpoint={endpoint}")
    return True


def traced(span_name: str):
    """Run the decorated function in a span of its own (a root span for a
    message handler)."""
    def decorate(func):
        tracer = trace.get_tracer(func.__module__)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(span_name):
                return func(*args, **kwargs)
        return wrapper
    return decorate


def annotate(**attributes) -> None:
    """Set attributes on the current span (none when tracing is off)."""
    trace.get_current_span().set_attributes(attributes)
