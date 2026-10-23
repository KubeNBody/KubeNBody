# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The OpenTelemetry helpers: off without an endpoint, one span per traced call."""
import pytest
from opentelemetry import trace
from opentelemetry.util.http import parse_excluded_urls
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tracing


@pytest.fixture(scope="module")
def exporter():
    # the global provider can be set once per process: the first test module
    # to set it keeps it
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    return exp


def test_off_without_an_endpoint(monkeypatch):
    monkeypatch.setattr(tracing, "_initialized", False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.setup_tracing("nbody-test") is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "false")
    assert tracing.setup_tracing("nbody-test") is False


def test_a_traced_call_records_one_span_with_its_attributes(exporter):
    @tracing.traced("merge epoch")
    def handler(job_id, epoch):
        tracing.annotate(job_id=job_id, epoch=epoch)
        return "done"

    exporter.clear()
    assert handler(7, 12) == "done"
    (span,) = exporter.get_finished_spans()
    assert span.name == "merge epoch"
    assert dict(span.attributes) == {"job_id": 7, "epoch": 12}


def test_an_exception_ends_the_span_as_an_error(exporter):
    @tracing.traced("finalize epoch")
    def handler():
        raise RuntimeError("boom")

    exporter.clear()
    with pytest.raises(RuntimeError):
        handler()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR


@pytest.mark.parametrize("path,untraced", [
    ("/metrics", True), ("/api/live", True), ("/api/ready", True), ("/api/health", True),
    ("/api/metrics", False), ("/api/comparison/jobs/1/metrics", False), ("/api/health/db", False),
    ("/api/jobs", False),
])
def test_only_the_probes_and_scrapes_are_untraced(path, untraced):
    excluded = parse_excluded_urls(tracing.UNTRACED_URLS)       # as instrument_app parses it
    assert excluded.url_disabled(f"http://dashboard-backend-svc:8000{path}") is untraced
