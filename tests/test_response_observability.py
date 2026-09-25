"""Response accounting uses reported usage, not nested span or content counts."""

import json
import unittest
from decimal import Decimal
from types import SimpleNamespace

from openai.types.responses import Response, ResponseUsage
from openai.types.responses.response_output_item import McpCall
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from notebook_support.response_observability import (
    load_model_pricing, price_response_usage, record_response_observability,
)


def usage_row(**changes):
    return {
        "UsageKey": "response/resource/response-1", "ResponseId": "response-1",
        "Interaction": "story", "Deployment": "demo-deployment", "Model": "model-version",
        "UsageState": "reported", "InputTokens": 1000, "OutputTokens": 100,
        "CachedInputTokens": 200, "ReasoningTokens": 40,
    } | changes


def pricing_json(**changes):
    return json.dumps({"demo-deployment": {
        "currency": "USD", "input_per_million": "2",
        "cached_input_per_million": "0.5", "output_per_million": "8",
        "source": "test fixture rates, not Azure prices",
    } | changes})


class PricingTests(unittest.TestCase):
    def test_unconfigured_pricing_is_explicit_not_zero_cost(self):
        for raw in (None, "", "{}"):
            with self.subTest(raw=raw):
                summary, details = price_response_usage([usage_row()], load_model_pricing(raw))
                self.assertIsNone(details[0]["EstimatedCost"])
                self.assertEqual(details[0]["CostState"], "pricing not configured")
                self.assertEqual(summary[0]["UnpricedResponses"], 1)
                self.assertIsNone(summary[0]["EstimatedCost"])

    def test_cached_and_reasoning_tokens_are_subsets_not_extra_charges(self):
        summary, details = price_response_usage([usage_row()], load_model_pricing(pricing_json()))
        self.assertEqual(details[0]["EstimatedCost"], Decimal("0.0025"))
        self.assertEqual(summary[0]["EstimatedCost"], Decimal("0.0025"))
        self.assertEqual(summary[0]["KnownInputTokens"], 1000)
        self.assertEqual(summary[0]["KnownOutputTokens"], 100)
        self.assertEqual(summary[0]["KnownCachedInputTokens"], 200)
        self.assertEqual(summary[0]["KnownReasoningTokens"], 40)
        self.assertEqual(summary[0]["EstimateCoverage"], "complete for reported responses")

    def test_missing_usage_keeps_partial_estimate_visible(self):
        rows = [usage_row(), usage_row(
            UsageKey="span/resource/failed", ResponseId="", UsageState="request failed",
            InputTokens=None, OutputTokens=None, CachedInputTokens=None, ReasoningTokens=None,
        )]
        summary, details = price_response_usage(rows, load_model_pricing(pricing_json()))
        self.assertEqual(summary[0]["EstimatedCost"], Decimal("0.0025"))
        self.assertEqual(summary[0]["UnpricedResponses"], 1)
        self.assertEqual(summary[0]["UsageMissing"], 1)
        self.assertEqual(summary[0]["EstimateCoverage"], "partial; excludes unpriced responses")
        self.assertIsNone(details[1]["EstimatedCost"])
        self.assertEqual(details[1]["CostState"], "request failed")

    def test_missing_cache_details_or_cache_price_does_not_invent_a_discount(self):
        cases = [
            (usage_row(CachedInputTokens=None), pricing_json(), "cached input usage not reported"),
            (usage_row(), pricing_json(cached_input_per_million=None), "cached input price not configured"),
        ]
        for row, raw, expected in cases:
            with self.subTest(expected=expected):
                _, details = price_response_usage([row], load_model_pricing(raw))
                self.assertIsNone(details[0]["EstimatedCost"])
                self.assertEqual(details[0]["CostState"], expected)

    def test_no_cached_tokens_does_not_require_cache_price(self):
        _, details = price_response_usage(
            [usage_row(CachedInputTokens=0)],
            load_model_pricing(pricing_json(cached_input_per_million=None)),
        )
        self.assertEqual(details[0]["EstimatedCost"], Decimal("0.0028"))

    def test_zero_usage_and_explicit_free_rates_are_valid(self):
        rows = [usage_row(InputTokens=0, OutputTokens=0, CachedInputTokens=0, ReasoningTokens=0)]
        summary, details = price_response_usage(rows, load_model_pricing(pricing_json()))
        self.assertEqual(details[0]["EstimatedCost"], Decimal(0))
        self.assertEqual(summary[0]["PricedResponses"], 1)
        _, free = price_response_usage(
            [usage_row()], load_model_pricing(pricing_json(
                input_per_million=0, cached_input_per_million=0, output_per_million=0,
            )),
        )
        self.assertEqual(free[0]["EstimatedCost"], Decimal(0))

    def test_invalid_or_conflicting_usage_is_never_priced(self):
        for changes in (
            {"InputTokens": -1}, {"OutputTokens": True}, {"CachedInputTokens": 1001},
            {"ReasoningTokens": 101}, {"InputTokens": "1000"},
            {"UsageState": "conflicting response snapshots"},
        ):
            with self.subTest(changes=changes):
                _, details = price_response_usage([usage_row(**changes)], load_model_pricing(pricing_json()))
                self.assertIsNone(details[0]["EstimatedCost"])
                self.assertNotEqual(details[0]["CostState"], "estimated")

    def test_deployment_price_precedes_model_price_and_currencies_do_not_mix(self):
        rates = json.loads(pricing_json())
        rates["model-version"] = rates["demo-deployment"] | {"input_per_million": "999"}
        rates["second-deployment"] = rates["demo-deployment"] | {"currency": "EUR"}
        rows = [usage_row(), usage_row(
            UsageKey="response/resource/response-2", ResponseId="response-2", Deployment="second-deployment",
        )]
        summary, details = price_response_usage(rows, load_model_pricing(json.dumps(rates)))
        self.assertEqual(len(summary), 2)
        self.assertEqual({row["Currency"] for row in summary}, {"USD", "EUR"})
        self.assertEqual(details[0]["PricingKey"], "demo-deployment")
        self.assertEqual(details[0]["EstimatedCost"], Decimal("0.0025"))

    def test_input_rows_are_not_modified_and_unknown_models_are_not_priced(self):
        row = usage_row(Deployment="unknown", Model="unknown")
        original = row.copy()
        _, details = price_response_usage([row], load_model_pricing(pricing_json()))
        self.assertEqual(row, original)
        self.assertEqual(details[0]["CostState"], "pricing not configured")

    def test_malformed_pricing_fails_explicitly(self):
        invalid = [
            "not JSON", "[]", '{"x": {}, "x": {}}', '{"x": {}}',
            pricing_json(input_per_million=-1), pricing_json(input_per_million=True),
            pricing_json(input_per_million="NaN"), pricing_json(output_per_million="Infinity"),
            pricing_json(currency=""), pricing_json(currency="usd"),
            pricing_json(input_per_milion="2"),
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    load_model_pricing(raw)

    def test_duplicate_canonical_rows_cannot_double_charge(self):
        with self.assertRaisesRegex(ValueError, "Duplicate canonical"):
            price_response_usage([usage_row(), usage_row()], load_model_pricing(pricing_json()))


class ResponseCaptureTests(unittest.TestCase):
    def setUp(self):
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("response-observability-test")
        self.addCleanup(self.provider.shutdown)

    def response(self, **changes):
        usage = ResponseUsage.model_construct(
            input_tokens=1000, output_tokens=100, total_tokens=1100,
            input_tokens_details=InputTokensDetails.model_construct(cached_tokens=200, cache_write_tokens=0),
            output_tokens_details=OutputTokensDetails.model_construct(reasoning_tokens=40),
        )
        return Response.model_construct(**{
            "id": "response-1", "model": "model-version", "status": "completed",
            "usage": usage, "output": [],
        } | changes)

    def capture(self, response=None, *, capture_content=False):
        with self.tracer.start_as_current_span("existing Responses dependency") as span:
            record_response_observability(
                span, response if response is not None else self.response(),
                deployment="demo-deployment", capture_content=capture_content,
            )
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        return spans[0]

    def test_usage_enriches_existing_span_without_recording_message_content(self):
        span = self.capture()
        assert span.attributes is not None
        self.assertEqual(span.attributes["app.usage.source"], "notebook.responses")
        self.assertEqual(span.attributes["gen_ai.response.id"], "response-1")
        self.assertEqual(span.attributes["gen_ai.usage.input_tokens"], 1000)
        self.assertEqual(span.attributes["gen_ai.usage.output_tokens"], 100)
        self.assertEqual(span.attributes["app.usage.cached_input_tokens"], 200)
        self.assertEqual(span.attributes["app.usage.reasoning_tokens"], 40)
        self.assertEqual(span.attributes["app.usage.state"], "reported")
        self.assertNotIn("gen_ai.input.messages", span.attributes)
        self.assertEqual(span.status.status_code, StatusCode.UNSET)

    def test_missing_usage_is_explicit(self):
        span = self.capture(self.response(usage=None))
        assert span.attributes is not None
        self.assertEqual(span.attributes["app.usage.state"], "not reported")
        self.assertNotIn("gen_ai.usage.input_tokens", span.attributes)
        self.assertEqual(span.attributes["app.response.status"], "completed")

    def test_missing_optional_usage_details_stay_unknown_without_failing_the_response(self):
        usage = ResponseUsage.model_construct(input_tokens=10, output_tokens=5, total_tokens=15)
        span = self.capture(self.response(usage=usage))
        assert span.attributes is not None
        self.assertEqual(span.attributes["app.usage.state"], "reported")
        self.assertNotIn("app.usage.cached_input_tokens", span.attributes)
        self.assertNotIn("app.usage.reasoning_tokens", span.attributes)

    def test_missing_output_is_not_reported_as_zero_tool_calls(self):
        span = self.capture(self.response(output=None))
        assert span.attributes is not None
        self.assertNotIn("app.mcp.tool_calls", span.attributes)
        self.assertEqual(span.attributes["app.mcp.state"], "not reported")

    def test_bad_usage_is_diagnostic_not_a_change_to_response_outcome(self):
        response = self.response()
        assert response.usage is not None
        response.usage.input_tokens = -1
        span = self.capture(response)
        assert span.attributes is not None
        self.assertEqual(span.attributes["app.usage.state"], "invalid")
        issue = span.attributes["app.usage.issue"]
        assert isinstance(issue, str)
        self.assertIn("input", issue)
        self.assertEqual(span.attributes["app.response.status"], "completed")
        self.assertEqual(span.status.status_code, StatusCode.UNSET)

    def test_zero_counts_are_not_treated_as_missing(self):
        response = self.response()
        assert response.usage is not None
        response.usage.input_tokens = response.usage.output_tokens = response.usage.total_tokens = 0
        response.usage.input_tokens_details.cached_tokens = 0
        response.usage.output_tokens_details.reasoning_tokens = 0
        span = self.capture(response)
        assert span.attributes is not None
        self.assertEqual(span.attributes["gen_ai.usage.input_tokens"], 0)
        self.assertEqual(span.attributes["app.usage.state"], "reported")

    def test_mcp_errors_are_visible_but_sensitive_detail_requires_capture(self):
        item = SimpleNamespace(
            type="mcp_call", id="tool-call-1", name="query", server_label="sentinel",
            error="PRIVATE_TOOL_ERROR",
        )
        span = self.capture(self.response(output=[item]))
        assert span.attributes is not None
        self.assertEqual(span.attributes["app.mcp.tool_calls"], 1)
        self.assertEqual(span.attributes["app.mcp.tool_errors"], 1)
        event = span.events[0]
        assert event.attributes is not None
        self.assertEqual(event.name, "mcp.tool.error")
        self.assertEqual(event.attributes["app.mcp.call.id"], "tool-call-1")
        self.assertNotIn("app.mcp.tool.name", event.attributes)
        self.assertNotIn("app.mcp.server", event.attributes)
        self.assertNotIn("PRIVATE_TOOL_ERROR", str(span.attributes) + str(event.attributes))
        self.assertEqual(span.status.status_code, StatusCode.UNSET)

    def test_tool_error_preview_is_bounded_when_enabled(self):
        item = SimpleNamespace(
            type="mcp_call", id="tool-call-1", name="query", server_label="sentinel",
            error="X" * 5000,
        )
        span = self.capture(self.response(output=[item]), capture_content=True)
        attributes = span.events[0].attributes
        assert attributes is not None
        detail = attributes["app.mcp.error.detail"]
        assert isinstance(detail, str)
        self.assertEqual(len(detail), 1200)
        self.assertTrue(attributes["app.mcp.error.detail_truncated"])

    def test_failed_tool_status_without_payload_is_still_visible(self):
        item = McpCall(
            type="mcp_call", id="tool-call-1", name="query", server_label="sentinel",
            arguments="{}", status="failed",
        )
        span = self.capture(self.response(output=[item]))
        assert span.attributes is not None
        assert span.events[0].attributes is not None
        self.assertEqual(span.attributes["app.mcp.tool_errors"], 1)
        self.assertEqual(span.events[0].attributes["app.mcp.tool.status"], "failed")
        self.assertNotIn("app.mcp.error.detail", span.events[0].attributes)

    def test_installed_azure_exporter_routes_span_events_to_trace_messages_with_exact_parent(self):
        from azure.monitor.opentelemetry.exporter._generated.exporter.models import MessageData
        from azure.monitor.opentelemetry.exporter.export.trace._exporter import _convert_span_events_to_envelopes

        item = McpCall(
            type="mcp_call", id="tool-call-1", name="query", server_label="sentinel",
            arguments="{}", status="failed",
        )
        span = self.capture(self.response(output=[item]))
        envelopes = _convert_span_events_to_envelopes(span)
        self.assertEqual(len(envelopes), 1)
        envelope = envelopes[0]
        assert envelope.data is not None
        assert envelope.tags is not None
        assert span.context is not None
        data = envelope.data.base_data
        assert isinstance(data, MessageData)
        assert data.properties is not None
        self.assertEqual(envelope.data.base_type, "MessageData")
        self.assertEqual(data.message, "mcp.tool.error")
        self.assertEqual(envelope.tags["ai.operation.parentId"], f"{span.context.span_id:016x}")
        self.assertEqual(data.properties["app.mcp.call.id"], "tool-call-1")


if __name__ == "__main__":
    unittest.main()
