"""OpenTelemetry setup for treadmill MCP."""
from __future__ import annotations
import logging, os

log = logging.getLogger("fitness_machine.otel")


def setup_otel() -> None:
    """Initialize OTEL if endpoint is configured. Silently skips if not available."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — skipping telemetry")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME

        service_name = os.environ.get("OTEL_SERVICE_NAME", "fitness-machine-mcp")
        resource_attrs = os.environ.get(
            "OTEL_RESOURCE_ATTRIBUTES",
            "service.namespace=ftms-mcp"
        )

        attrs = {"service.name": service_name}
        for kv in resource_attrs.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                attrs[k.strip()] = v.strip()

        resource = Resource.create(attrs)
        provider = TracerProvider(resource=resource)

        otlp_endpoint = endpoint.rstrip("/") + "/v1/traces"
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        log.info("OTEL tracing enabled → %s (service=%s)", endpoint, service_name)
    except Exception as e:
        log.warning("OTEL setup failed (non-fatal): %s", e)
