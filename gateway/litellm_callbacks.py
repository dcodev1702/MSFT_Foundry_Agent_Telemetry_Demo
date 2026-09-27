"""LiteLLM logging hook that describes the gateway's Foundry agent calls in LiteLLM's own spans.

Generic pass-through routes give LiteLLM no provider, model, usage or result, so its spans report
``gen_ai.system=Unknown``, ``gen_ai.request.model=unknown`` and empty ``hidden_params``. LiteLLM runs
``async_logging_hook`` after building its standard logging payload and before any success callback,
including ``otel``, so this hook fills those fields from the Foundry request and response instead.
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
# LiteLLM's provider ID for Microsoft Foundry. LiteLLM prices Foundry's Azure OpenAI models as azure/<model>.
PROVIDER = "azure_ai"
PRICING_PROVIDER = "azure"
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


def _content_parts(content: object) -> object:
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if isinstance(part, Mapping) and part.get("type") in {"input_text", "output_text", "text"}:
            parts.append({"type": "text", "content": part.get("text", "")})
        else:
            parts.append(part)
    return parts


def input_messages(request: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Return a Responses or Conversations request's input as chat-style messages."""
    items = request.get("input", request.get("items"))
    if isinstance(items, str):
        return [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        return None
    messages = []
    for item in items:
        if isinstance(item, Mapping) and isinstance(item.get("role"), str) and "content" in item:
            messages.append({"role": item["role"], "content": _content_parts(item["content"])})
        else:
            # MCP approvals and other non-message items keep their full JSON.
            messages.append({"role": "user", "content": json.dumps(item, default=str)})
    return messages


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

    params = kwargs.setdefault("litellm_params", {})
    params["custom_llm_provider"] = PROVIDER
    if request_model:
        kwargs["model"] = request_model
        payload["model"] = request_model
    payload["custom_llm_provider"] = PROVIDER
    payload["model_id"] = endpoint["agent"]
    if messages := input_messages(request):
        kwargs["messages"] = messages
    if isinstance(request.get("instructions"), str):
        kwargs["instructions"] = request["instructions"]
    # Feeds LiteLLM's raw_gen_ai_request span (llm.azure_ai.* request and response attributes).
    additional_args = kwargs.get("additional_args")
    if not isinstance(additional_args, dict):
        additional_args = kwargs["additional_args"] = {}
    additional_args.setdefault("complete_input_dict", dict(request))
    if response is not None:
        kwargs["original_response"] = json.dumps(response, default=str)

    if usage is not None:
        payload.update(
            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
        )
        metadata["usage_object"] = usage
    if cost is not None:
        payload["response_cost"] = cost
        kwargs["response_cost"] = cost
    headers = (params.get("proxy_server_request") or {}).get("headers") or {}
    user_agent = next((value for key, value in headers.items() if str(key).lower() == "user-agent"), None)
    if user_agent and not metadata.get("user_agent"):
        metadata["user_agent"] = user_agent

    hidden = payload.get("hidden_params")
    if not isinstance(hidden, dict):
        hidden = payload["hidden_params"] = {}
    hidden.update(
        model_id=endpoint["agent"],
        api_base=endpoint["base"],
        litellm_model_name=f"{PROVIDER}/{model}" if model else None,
        usage_object=usage,
        response_cost=cost,
        litellm_overhead_time_ms=_overhead_ms(kwargs),
    )

    if response is not None and isinstance(result, dict):
        result.update({key: response[key] for key in ("id", "object", "model", "status", "output") if key in response})
        if usage is not None:
            result["usage"] = usage
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
