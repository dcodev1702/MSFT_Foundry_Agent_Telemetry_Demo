"""LiteLLM logging hook that describes the gateway's Foundry agent calls in LiteLLM's OTel v2 spans.

Generic pass-through routes give LiteLLM no provider, model, usage or result, so its ``chat`` span
would carry no model, the raw request body as the prompt and no output. LiteLLM runs
``async_logging_hook`` after building its standard logging payload and before any success callback,
including ``otel``. This hook fills the payload fields that LiteLLM's OpenTelemetry v2 mappers read,
from the Foundry request and response:

- ``model`` and ``custom_llm_provider``: the span name ``chat <model>``, ``gen_ai.request.model``
  and ``gen_ai.provider.name``;
- ``messages``: ``gen_ai.input.messages``, with the request's instructions as a system message;
- ``response["choices"]``: ``gen_ai.output.messages`` and ``gen_ai.response.finish_reasons``;
- token counts and ``metadata.usage_object``: ``gen_ai.usage.*``, including cached input tokens;
- ``response_cost``: ``litellm.cost.total``;
- ``hidden_params``: ``server.address``, ``server.port`` and ``litellm.provider.model``.

Messages use the OpenTelemetry GenAI format, ``[{"role": ..., "parts": [...]}]``, with the part
types that the Azure AI Projects instrumentor uses for Responses items, so Application Insights
stores them in AppGenAIContent like the notebook's and Foundry's content.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
import json
import os
import re
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # Unit tests import this module without LiteLLM installed.
    CustomLogger = object

ROUTE_PREFIX = "/foundry-agent/"
# gen_ai.provider.name on LiteLLM's span: the value that Foundry's own spans in the trace report.
PROVIDER = "microsoft.foundry"
# LiteLLM's provider ID for Microsoft Foundry, used in LiteLLM model names such as azure_ai/<model>.
LITELLM_PROVIDER = "azure_ai"
# LiteLLM prices Foundry's Azure OpenAI models as azure/<model>.
PRICING_PROVIDER = "azure"
_TEXT_PARTS = {"input_text", "output_text", "text"}
# Tool and MCP item fields kept on output parts, as the Azure AI Projects instrumentor keeps them.
_TOOL_FIELDS = (
    "id", "call_id", "name", "server_label", "arguments", "approval_request_id", "approve",
    "status", "error",
)
_AGENT_ENDPOINT = re.compile(
    r"^(?P<base>[^?#]*/agents/(?P<agent>[^/?#]+)/endpoint/protocols/openai)(?:/(?P<operation>[^/?#]+))?"
)
CostFunction = Callable[..., tuple[float, float]]


def responses_usage_to_chat(usage: object) -> dict[str, Any] | None:
    """Convert Responses API usage to the chat-completion keys that LiteLLM's loggers read."""
    if not isinstance(usage, Mapping):
        return None
    prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
    completion = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    total = usage.get("total_tokens")
    converted: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total if isinstance(total, int) else prompt + completion,
    }
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens")
    reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    if isinstance(cached, int):
        converted["prompt_tokens_details"] = {"cached_tokens": cached}
    if isinstance(reasoning, int):
        converted["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return converted


def _parts(content: object) -> list[dict[str, Any]]:
    """Return Responses message content as OpenTelemetry GenAI message parts."""
    if isinstance(content, str):
        return [{"type": "text", "content": content}] if content else []
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in _TEXT_PARTS:
            parts.append({"type": "text", "content": part.get("text", "")})
        elif part.get("type") == "refusal":
            parts.append({"type": "text", "content": part.get("refusal", "")})
        else:
            parts.append({"type": str(part.get("type") or "unknown"), "content": dict(part)})
    return parts


def input_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a Responses or Conversations request's instructions and input as GenAI messages."""
    messages = []
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "parts": [{"type": "text", "content": instructions}]})
    items = request.get("input", request.get("items"))
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("role"), str) and "content" in item:
            messages.append({"role": item["role"], "parts": _parts(item["content"])})
        else:
            # MCP approvals and other non-message items, whole, like the Projects instrumentor.
            item_type = str(item.get("type") or "unknown")
            part_type = "mcp" if item_type.startswith("mcp_") else item_type
            messages.append({"role": "user", "parts": [{"type": part_type, "content": dict(item)}]})
    return messages


def _finish_reason(response: Mapping[str, Any]) -> str | None:
    status = response.get("status")
    if status == "completed":
        output = response.get("output") or []
        waiting = any(
            isinstance(item, Mapping) and item.get("type") == "mcp_approval_request"
            for item in output
        )
        return "tool_call" if waiting else "stop"
    if status == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        return "content_filter" if reason == "content_filter" else "length"
    if status == "failed":
        return "error"
    return status if isinstance(status, str) else None


def output_choices(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a Responses API response's output as one chat choice, the shape LiteLLM v2 reads."""
    parts = []
    for item in response.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "message":
            parts.extend(_parts(item.get("content")))
        elif item_type.startswith("mcp_") or item_type.endswith("_call"):
            fields = {field: item[field] for field in _TOOL_FIELDS if item.get(field) is not None}
            parts.append({"type": "tool_call", "content": {"type": item_type, **fields}})
    if not parts:
        return []
    reason = _finish_reason(response)
    message: dict[str, Any] = {"role": "assistant", "parts": parts}
    if reason:
        message["finish_reason"] = reason
    return [{"index": 0, "finish_reason": reason, "message": message}]


def _response_body(payload: Mapping[str, Any], result: object) -> dict[str, Any] | None:
    body = payload.get("response_body")
    if isinstance(body, dict):
        return body
    text = result.get("response") if isinstance(result, Mapping) else None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _overhead_ms(kwargs: Mapping[str, Any]) -> float | None:
    start, forwarded = kwargs.get("start_time"), kwargs.get("api_call_start_time")
    if isinstance(start, datetime) and isinstance(forwarded, datetime) and forwarded >= start:
        return round((forwarded - start).total_seconds() * 1000, 3)
    return None


def _request_cost(
    model: str | None, usage: Mapping[str, Any] | None, cost_per_token: CostFunction | None,
) -> float | None:
    if not model or not usage or cost_per_token is None:
        return None
    try:
        prompt_cost, completion_cost = cost_per_token(
            model=f"{PRICING_PROVIDER}/{model}",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cache_read_input_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        )
    except Exception:  # noqa: BLE001 - an unpriced model must not block logging.
        return None
    return float(prompt_cost) + float(completion_cost)


def enrich_foundry_passthrough(
    kwargs: dict[str, Any], result: object, *, model_deployment: str | None,
    cost_per_token: CostFunction | None = None,
) -> tuple[dict[str, Any], object]:
    """Fill LiteLLM's logging payload for one Foundry agent pass-through call, in place."""
    payload = kwargs.get("standard_logging_object")
    passthrough = kwargs.get("passthrough_logging_payload") or {}
    if kwargs.get("call_type") != "pass_through_endpoint" or not isinstance(payload, dict):
        return kwargs, result
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return kwargs, result
    route = metadata.get("user_api_key_request_route") or ""
    endpoint = _AGENT_ENDPOINT.match(str(passthrough.get("url") or ""))
    if not str(route).startswith(ROUTE_PREFIX) or endpoint is None:
        return kwargs, result

    request = passthrough.get("request_body") if isinstance(passthrough.get("request_body"), dict) else {}
    response = _response_body(passthrough, result)
    response_model = response.get("model") if response and isinstance(response.get("model"), str) else None
    request_model = request.get("model") if isinstance(request.get("model"), str) else model_deployment
    model = response_model or request_model
    usage = responses_usage_to_chat(response.get("usage")) if response else None
    cost = _request_cost(model, usage, cost_per_token)
    if cost is None and usage is None:
        cost = payload.get("response_cost")

    kwargs.setdefault("litellm_params", {})["custom_llm_provider"] = LITELLM_PROVIDER
    if request_model:
        kwargs["model"] = request_model
        payload["model"] = request_model
    payload["custom_llm_provider"] = PROVIDER
    payload["model_id"] = endpoint["agent"]
    # A request without input, such as creating a conversation, records no prompt.
    payload["messages"] = input_messages(request)
    if response is not None:
        payload["response"] = {**response, "choices": output_choices(response)}

    if usage is not None:
        payload.update(
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
        )
        metadata["usage_object"] = usage
    if cost is not None:
        payload["response_cost"] = cost
        kwargs["response_cost"] = cost

    hidden = payload.get("hidden_params")
    if not isinstance(hidden, dict):
        hidden = payload["hidden_params"] = {}
    hidden.update(
        model_id=endpoint["agent"],
        api_base=endpoint["base"],
        litellm_model_name=f"{LITELLM_PROVIDER}/{model}" if model else None,
        usage_object=usage,
        response_cost=cost,
        litellm_overhead_time_ms=_overhead_ms(kwargs),
    )

    return kwargs, result


class FoundryAgentTelemetry(CustomLogger):
    """Fill provider, model, usage, cost and result fields for Foundry agent pass-through logs."""

    def __init__(self) -> None:
        if CustomLogger is not object:
            super().__init__()
        self.model_deployment = os.getenv("FOUNDRY_MODEL_DEPLOYMENT") or None

    def _enrich(self, kwargs: dict[str, Any], result: object) -> tuple[dict[str, Any], object]:
        try:
            from litellm import cost_per_token
        except ImportError:
            cost_per_token = None
        try:
            return enrich_foundry_passthrough(
                kwargs, result, model_deployment=self.model_deployment, cost_per_token=cost_per_token,
            )
        except Exception:  # noqa: BLE001 - telemetry enrichment must never fail a request's logging.
            return kwargs, result

    async def async_logging_hook(self, kwargs: dict, result: object, call_type: str) -> tuple[dict, object]:
        return self._enrich(kwargs, result)

    def logging_hook(self, kwargs: dict, result: object, call_type: str) -> tuple[dict, object]:
        return self._enrich(kwargs, result)


foundry_agent_telemetry = FoundryAgentTelemetry()
