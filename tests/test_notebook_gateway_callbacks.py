"""LiteLLM logging hook that fills the OpenTelemetry v2 span fields for the gateway's Foundry agent calls."""

from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("litellm_callbacks", ROOT / "gateway" / "litellm_callbacks.py")
assert _spec is not None and _spec.loader is not None
callbacks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(callbacks)

AGENT_BASE = "https://demo.services.ai.azure.com/api/projects/demo/agents/ZoDEfendersAgent-1702-backend/endpoint/protocols/openai"
HIDDEN_KEYS = (
    "model_id", "cache_key", "api_base", "response_cost", "additional_headers", "litellm_overhead_time_ms",
    "batch_models", "batch_successful_requests", "batch_failed_requests", "litellm_model_name", "usage_object",
)
START = datetime(2026, 9, 27, 10, 0, 0)
RESPONSE = {
    "id": "resp_test_001", "object": "response", "status": "completed", "model": "gpt-5.6-terra-2026-07-09",
    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi."}]}],
    "usage": {"input_tokens": 120, "input_tokens_details": {"cached_tokens": 20},
              "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 10}, "total_tokens": 150},
}


def passthrough_kwargs(operation, request_body, response_body, route_prefix="/foundry-agent/main"):
    """The logging payload LiteLLM 1.102.1 hands to async_logging_hook for a generic pass-through call."""
    return {
        "call_type": "pass_through_endpoint", "model": "unknown",
        "messages": [{"role": "user", "content": json.dumps(request_body)}],
        "start_time": START, "api_call_start_time": START + timedelta(milliseconds=2.5),
        "additional_args": {},
        "litellm_params": {
            "api_base": f"{AGENT_BASE}/{operation}?api-version=v1", "metadata": {},
            "proxy_server_request": {"headers": {"User-Agent": "OpenAI/Python 3.19.2"}},
        },
        "passthrough_logging_payload": {
            "url": f"{AGENT_BASE}/{operation}", "request_method": "POST",
            "request_body": request_body, "response_body": response_body,
        },
        "standard_logging_object": {
            "model": "unknown", "model_id": "", "custom_llm_provider": None, "response_cost": 0.0,
            "metadata": {
                "user_api_key_request_route": f"{route_prefix}/{operation}", "user_agent": None,
                "usage_object": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
            "hidden_params": dict.fromkeys(HIDDEN_KEYS),
        },
    }


class FakeCost:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return 0.25, 0.5


class FoundryPassThroughEnrichmentTests(unittest.TestCase):
    def enrich(self, kwargs, result, cost=None):
        return callbacks.enrich_foundry_passthrough(
            kwargs, result, model_deployment="gpt-5.6-terra", cost_per_token=cost or FakeCost(),
        )

    def test_responses_call_fills_the_fields_litellm_otel_v2_reads(self):
        cost = FakeCost()
        request = {"input": "Tell me a story.", "conversation": "conv_1", "instructions": "Be brief."}
        kwargs = passthrough_kwargs("responses", request, RESPONSE)
        raw_messages = deepcopy(kwargs["messages"])
        kwargs, result = self.enrich(kwargs, {"response": "…"}, cost)
        payload, hidden = kwargs["standard_logging_object"], kwargs["standard_logging_object"]["hidden_params"]
        self.assertEqual((kwargs["model"], kwargs["litellm_params"]["custom_llm_provider"]), ("gpt-5.6-terra", "azure_ai"))
        # gen_ai.provider.name matches Foundry's own spans; LiteLLM model names keep its azure_ai provider ID.
        self.assertEqual((payload["model"], payload["model_id"], payload["custom_llm_provider"]),
                         ("gpt-5.6-terra", "ZoDEfendersAgent-1702-backend", "microsoft.foundry"))
        usage = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
                 "prompt_tokens_details": {"cached_tokens": 20}, "completion_tokens_details": {"reasoning_tokens": 10}}
        self.assertEqual(hidden, {
            **dict.fromkeys(HIDDEN_KEYS), "model_id": "ZoDEfendersAgent-1702-backend", "api_base": AGENT_BASE,
            "litellm_model_name": "azure_ai/gpt-5.6-terra-2026-07-09", "usage_object": usage,
            "response_cost": 0.75, "litellm_overhead_time_ms": 2.5,
        })
        self.assertEqual(cost.calls, [{
            "model": "azure/gpt-5.6-terra-2026-07-09", "prompt_tokens": 120, "completion_tokens": 30,
            "cache_read_input_tokens": 20,
        }])
        self.assertEqual((payload["prompt_tokens"], payload["completion_tokens"], payload["total_tokens"]), (120, 30, 150))
        self.assertEqual((payload["response_cost"], kwargs["response_cost"]), (0.75, 0.75))
        self.assertEqual(payload["metadata"]["usage_object"], usage)
        # gen_ai.input.messages: the instructions as a system message, then the input.
        self.assertEqual(payload["messages"], [
            {"role": "system", "parts": [{"type": "text", "content": "Be brief."}]},
            {"role": "user", "parts": [{"type": "text", "content": "Tell me a story."}]},
        ])
        # gen_ai.output.messages, finish reasons, response ID and model.
        self.assertEqual(payload["response"]["choices"], [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "parts": [{"type": "text", "content": "Hi."}], "finish_reason": "stop"},
        }])
        self.assertEqual((payload["response"]["id"], payload["response"]["model"]),
                         ("resp_test_001", "gpt-5.6-terra-2026-07-09"))
        # The client's result and LiteLLM's own request fields are left alone.
        self.assertEqual(result, {"response": "…"})
        self.assertEqual(kwargs["messages"], raw_messages)
        self.assertNotIn("original_response", kwargs)
        self.assertEqual(kwargs["additional_args"], {})

    def test_conversation_call_records_the_agent_and_model_without_a_prompt(self):
        response = {"id": "conv_test_001", "object": "conversation", "created_at": 1, "metadata": {}}
        kwargs, _ = self.enrich(passthrough_kwargs("conversations", {}, response), {"response": "…"})
        payload = kwargs["standard_logging_object"]
        hidden = payload["hidden_params"]
        self.assertEqual(kwargs["model"], "gpt-5.6-terra")
        self.assertEqual(hidden["litellm_model_name"], "azure_ai/gpt-5.6-terra")
        self.assertEqual((hidden["model_id"], hidden["usage_object"], hidden["response_cost"]),
                         ("ZoDEfendersAgent-1702-backend", None, 0.0))
        self.assertEqual(payload["messages"], [])
        self.assertEqual((payload["response"]["id"], payload["response"]["choices"]), ("conv_test_001", []))

    def test_unpriced_model_keeps_usage_and_leaves_the_cost_unknown(self):
        kwargs, _ = self.enrich(
            passthrough_kwargs("responses", {"input": "Hi"}, RESPONSE), {"response": "…"}, FakeCost(KeyError("model")),
        )
        payload = kwargs["standard_logging_object"]
        self.assertIsNone(payload["hidden_params"]["response_cost"])
        self.assertEqual(payload["response_cost"], 0.0)
        self.assertEqual(payload["total_tokens"], 150)

    def test_other_routes_and_call_types_are_left_unchanged(self):
        other_route = passthrough_kwargs("responses", {"input": "Hi"}, RESPONSE, route_prefix="/other")
        completion = {**passthrough_kwargs("responses", {"input": "Hi"}, RESPONSE), "call_type": "acompletion"}
        for kwargs in (other_route, completion):
            with self.subTest(route=kwargs["standard_logging_object"]["metadata"]["user_api_key_request_route"],
                              call_type=kwargs["call_type"]):
                original = deepcopy(kwargs)
                self.assertEqual(self.enrich(kwargs, {"response": "…"}), (original, {"response": "…"}))

    def test_response_text_is_parsed_when_litellm_kept_no_response_body(self):
        kwargs = passthrough_kwargs("responses", {"input": "Hi"}, None)
        kwargs, _ = self.enrich(kwargs, {"response": json.dumps(RESPONSE)})
        payload = kwargs["standard_logging_object"]
        self.assertEqual((payload["response"]["id"], payload["total_tokens"]), ("resp_test_001", 150))

    def test_input_items_become_genai_messages_and_approvals_mcp_parts(self):
        approval = {"type": "mcp_approval_response", "approve": True, "approval_request_id": "mcpr_1"}
        messages = callbacks.input_messages({"input": [
            {"role": "user", "content": [{"type": "input_text", "text": "Query Sentinel."}]}, approval,
        ]})
        self.assertEqual(messages, [
            {"role": "user", "parts": [{"type": "text", "content": "Query Sentinel."}]},
            {"role": "user", "parts": [{"type": "mcp", "content": approval}]},
        ])
        self.assertEqual(callbacks.input_messages({}), [])

    def test_tool_calls_and_approvals_become_tool_call_parts_with_finish_reasons(self):
        call = {"id": "mcp_1", "type": "mcp_call", "server_label": "msft-learn", "name": "microsoft_docs_search",
                "arguments": "{}", "output": "long tool result", "status": "completed"}
        request = {"id": "mcpr_2", "type": "mcp_approval_request", "server_label": "msft-learn",
                   "name": "microsoft_docs_search", "arguments": "{}"}
        choice, = callbacks.output_choices({"status": "completed", "output": [call, request]})
        self.assertEqual(choice["finish_reason"], "tool_call")
        parts = choice["message"]["parts"]
        # Tool output is left out, as the Azure AI Projects instrumentor leaves it out.
        self.assertEqual(parts[0], {"type": "tool_call", "content": {
            "type": "mcp_call", "id": "mcp_1", "name": "microsoft_docs_search", "server_label": "msft-learn",
            "arguments": "{}", "status": "completed",
        }})
        self.assertEqual(parts[1]["content"]["type"], "mcp_approval_request")
        text = [{"type": "message", "content": [{"type": "output_text", "text": "Partial."}]}]
        for response, reason in (
            ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "length"),
            ({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, "content_filter"),
            ({"status": "failed"}, "error"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(callbacks.output_choices({**response, "output": text})[0]["finish_reason"], reason)
        self.assertEqual(callbacks.output_choices({"status": "completed", "output": []}), [])

    def test_hook_never_breaks_logging_and_is_the_configured_instance(self):
        hook = callbacks.foundry_agent_telemetry
        broken = {"call_type": "pass_through_endpoint", "standard_logging_object": {"metadata": None}}
        self.assertEqual(hook.logging_hook(broken, "result", "pass_through_endpoint"), (broken, "result"))
        self.assertIsInstance(hook, callbacks.FoundryAgentTelemetry)


if __name__ == "__main__":
    unittest.main()
