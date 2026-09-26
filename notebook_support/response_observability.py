"""Response accounting, opt-in MCP content observations, and explicit token pricing."""

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, TypeGuard

from openai.types.responses import Response
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind


Row = dict[str, Any]
ERROR_PREVIEW_LIMIT = 1200
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenPricing:
    currency: str
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Decimal | None = None
    source: str = ""


def _unique_object(pairs: list[tuple[str, Any]]) -> Row:
    result: Row = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate pricing key: {key!r}.")
        result[key] = value
    return result


def _rate(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a finite, non-negative per-million-token rate.")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal rate.") from error
    if not rate.is_finite() or rate < 0:
        raise ValueError(f"{name} must be finite and non-negative.")
    return rate


def load_model_pricing(raw: str | None) -> dict[str, TokenPricing]:
    """Read explicit rates keyed by deployment name or exact response model."""
    if raw is None or not raw.strip():
        return {}
    try:
        values = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise ValueError("NOTEBOOK_MODEL_PRICING_JSON must be a JSON object.") from error
    if not isinstance(values, dict):
        raise ValueError("NOTEBOOK_MODEL_PRICING_JSON must be a JSON object.")
    required = {"currency", "input_per_million", "output_per_million"}
    allowed = required | {"cached_input_per_million", "source"}
    pricing = {}
    for name, entry in values.items():
        if not name or name != name.strip() or not isinstance(entry, dict):
            raise ValueError("Each pricing entry requires an exact, non-empty model/deployment name and an object.")
        if required - entry.keys() or entry.keys() - allowed:
            raise ValueError(f"Pricing for {name!r} requires {sorted(required)}; optional fields: cached_input_per_million, source.")
        currency = entry["currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"Pricing currency for {name!r} must be a three-letter uppercase code.")
        source = entry.get("source", "")
        if not isinstance(source, str):
            raise ValueError(f"Pricing source for {name!r} must be text.")
        cached = entry.get("cached_input_per_million")
        pricing[name] = TokenPricing(
            currency=currency,
            input_per_million=_rate(entry["input_per_million"], f"{name}.input_per_million"),
            output_per_million=_rate(entry["output_per_million"], f"{name}.output_per_million"),
            cached_input_per_million=None if cached is None else _rate(cached, f"{name}.cached_input_per_million"),
            source=source,
        )
    return pricing


def _is_token_count(value: object) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _usage_problem(input_tokens: Any, output_tokens: Any, cached: Any, reasoning: Any) -> str:
    if not _is_token_count(input_tokens) or not _is_token_count(output_tokens):
        return "input/output token counts must be non-negative integers"
    for name, value, total in (("cached input", cached, input_tokens), ("reasoning", reasoning, output_tokens)):
        if value is not None and (not _is_token_count(value) or value > total):
            return f"{name} tokens must be a non-negative subset of their token total"
    return ""


def get_tool_content_recording_policy(
    *, content_enabled: bool, environment: Mapping[str, str] | None = None,
) -> bool:
    """Apply the notebook's tool-content opt-in under its master content policy."""
    if not isinstance(content_enabled, bool):
        raise TypeError("content_enabled must be a boolean.")
    environment = os.environ if environment is None else environment
    value = environment.get("OTEL_LOG_TOOL_CONTENT", "0").strip().lower()
    if value not in {"0", "1", "false", "true"}:
        raise ValueError("OTEL_LOG_TOOL_CONTENT must be '0', '1', 'false', or 'true'.")
    return content_enabled and value in {"1", "true"}


def _record_tool_content(
    parent: Span, response: Response, *, enabled: bool,
    deployment: str, conversation_id: str | None,
) -> None:
    parent.set_attribute("app.tool.content.enabled", enabled)
    if not enabled:
        parent.set_attribute("app.tool.content.state", "disabled")
        return
    output = getattr(response, "output", None)
    if output is None:
        parent.set_attribute("app.tool.content.state", "response output not reported")
        return
    items = [item for item in output if item.type in {"mcp_call", "mcp_approval_request", "mcp_list_tools"}]
    parent.set_attribute("app.tool.content.items", len(items))
    parent.set_attribute(
        "app.tool.content.state", "observed" if items else "no MCP items returned",
    )
    tracer = trace.get_tracer(__name__)
    for item in items:
        snapshot = item.model_dump(mode="json")
        response_id = getattr(response, "id", None) or ""
        error = snapshot.get("error")
        status = snapshot.get("status") or {
            "mcp_call": "not reported",
            "mcp_approval_request": "approval requested",
            "mcp_list_tools": "discovery returned",
        }[item.type]
        returned_error = (error is not None and error != "") or status in {"failed", "incomplete"}
        attributes: dict[str, str | bool | int] = {
            "app.tool.observation": True,
            "app.tool.source": "responses.output",
            "app.tool.timing": "client observation; remote duration unavailable",
            "app.tool.output_item.type": item.type,
            "app.tool.output_item.id": item.id,
            "app.tool.returned_error": returned_error,
            "gen_ai.operation.name": "observe_tool",
            "gen_ai.response.id": response_id,
            "app.model.deployment": deployment,
            "app.mcp.server": item.server_label,
            "app.mcp.tool.status": status,
        }
        if conversation_id:
            attributes["gen_ai.conversation.id"] = conversation_id
        model = getattr(response, "model", None)
        if model:
            attributes["gen_ai.response.model"] = model
        if item.type == "mcp_call":
            attributes["gen_ai.tool.call.id"] = item.id
            if item.approval_request_id:
                attributes["app.mcp.approval.id"] = item.approval_request_id
        elif item.type == "mcp_approval_request":
            attributes["app.mcp.approval.id"] = item.id
        if item.type != "mcp_list_tools":
            attributes["gen_ai.tool.name"] = item.name
            attributes["gen_ai.tool.type"] = "extension"
        for field, key, label in (
            ("arguments", "gen_ai.tool.call.arguments", "arguments"),
            ("output", "gen_ai.tool.call.result", "result"),
            ("tools", "gen_ai.tool.definitions", "definitions"),
        ):
            value = snapshot.get(field)
            attributes[f"app.tool.{label}.present"] = value is not None
            if value is not None:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                attributes[key] = text
                attributes[f"app.tool.{label}.characters"] = len(text)
        # These spans observe returned items, not the unobservable remote execution.
        with tracer.start_as_current_span(
            f"notebook.mcp.observe {item.type}",
            context=trace.set_span_in_context(parent),
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        ) as observation:
            observation.add_event("mcp.tool.content.observed", {
                "app.mcp.event.id": f"{response_id}:{item.id}:content",
                "app.response.id": response_id,
                "app.tool.output_item.id": item.id,
                "app.tool.output_item.type": item.type,
                "app.tool.returned_error": returned_error,
            })


def record_response_observability(
    span: Span, response: Response, *, deployment: str, capture_content: bool,
    capture_tool_content: bool | None = None, conversation_id: str | None = None,
) -> None:
    """Enrich the existing request span without changing the response or outcome."""
    if not isinstance(capture_content, bool):
        raise TypeError("capture_content must be a boolean.")
    if capture_tool_content is not None and not isinstance(capture_tool_content, bool):
        raise TypeError("capture_tool_content must be a boolean or None.")
    span.set_attributes({
        "app.usage.source": "notebook.responses",
        "app.model.deployment": deployment,
        "app.response.status": getattr(response, "status", None) or "not reported",
    })
    response_id = getattr(response, "id", "") or ""
    for attribute, field in (("gen_ai.response.id", "id"), ("gen_ai.response.model", "model")):
        value = getattr(response, field, None)
        if isinstance(value, str) and value:
            span.set_attribute(attribute, value)
    output = getattr(response, "output", None)
    tool_calls = tool_errors = 0
    for item in output or []:
        if item.type != "mcp_call":
            continue
        tool_calls += 1
        error = getattr(item, "error", None)
        status = getattr(item, "status", None)
        if (error is None or error == "") and status not in {"failed", "incomplete"}:
            continue
        tool_errors += 1
        attributes: dict[str, str | bool] = {
            "app.mcp.event.id": f"{response_id}:{item.id}:tool_error",
            "app.response.id": response_id,
            "app.mcp.call.id": item.id,
        }
        if isinstance(status, str):
            attributes["app.mcp.tool.status"] = status
        if capture_content:
            for attribute, field in (("app.mcp.server", "server_label"), ("app.mcp.tool.name", "name")):
                value = getattr(item, field, None)
                if isinstance(value, str):
                    attributes[attribute] = value
            detail = str(error) if error is not None else f"Tool status: {status}; no error payload returned."
            attributes["app.mcp.error.detail"] = detail[:ERROR_PREVIEW_LIMIT]
            attributes["app.mcp.error.detail_truncated"] = len(detail) > ERROR_PREVIEW_LIMIT
        span.add_event("mcp.tool.error", attributes)
    if output is not None:
        span.set_attributes({"app.mcp.tool_calls": tool_calls, "app.mcp.tool_errors": tool_errors})
    else:
        span.set_attribute("app.mcp.state", "not reported")

    if capture_tool_content is not None:
        _record_tool_content(
            span, response, enabled=capture_content and capture_tool_content,
            deployment=deployment, conversation_id=conversation_id,
        )

    usage = getattr(response, "usage", None)
    if usage is None:
        span.set_attribute("app.usage.state", "not reported")
        return
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None)
    reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None)
    problem = _usage_problem(input_tokens, output_tokens, cached, reasoning)
    if (
        not problem and _is_token_count(input_tokens) and _is_token_count(output_tokens)
        and total_tokens is not None
        and (not _is_token_count(total_tokens) or total_tokens != input_tokens + output_tokens)
    ):
        problem = "total_tokens does not equal input + output tokens"
    span.set_attribute("app.usage.state", "invalid" if problem else "reported")
    if problem:
        span.set_attribute("app.usage.issue", problem)
    for name, value in (
        ("gen_ai.usage.input_tokens", input_tokens),
        ("gen_ai.usage.output_tokens", output_tokens),
        ("app.usage.cached_input_tokens", cached),
        ("app.usage.reasoning_tokens", reasoning),
    ):
        if _is_token_count(value):
            span.set_attribute(name, value)


def _estimate(row: Row, rates: TokenPricing | None) -> tuple[Decimal | None, str]:
    if row["UsageState"] != "reported":
        return None, row["UsageState"]
    problem = _usage_problem(
        row["InputTokens"], row["OutputTokens"], row["CachedInputTokens"], row["ReasoningTokens"],
    )
    if problem:
        return None, f"invalid usage: {problem}"
    if rates is None:
        return None, "pricing not configured"
    cached = row["CachedInputTokens"]
    if cached is None:
        return None, "cached input usage not reported"
    if cached and rates.cached_input_per_million is None:
        return None, "cached input price not configured"
    cached_cost = Decimal(0)
    if cached and rates.cached_input_per_million is not None:
        cached_cost = Decimal(cached) * rates.cached_input_per_million
    cost = (
        Decimal(row["InputTokens"] - cached) * rates.input_per_million
        + cached_cost + Decimal(row["OutputTokens"]) * rates.output_per_million
    ) / MILLION
    return cost, "estimated"


def price_response_usage(
    rows: Sequence[Row], pricing: Mapping[str, TokenPricing],
) -> tuple[list[Row], list[Row]]:
    """Price canonical response rows; keep missing usage/rates and currencies explicit."""
    summaries: dict[tuple[str, ...], Row] = {}
    details = []
    seen = set()
    for row in rows:
        if row["UsageKey"] in seen:
            raise ValueError(f"Duplicate canonical response usage key: {row['UsageKey']}.")
        seen.add(row["UsageKey"])
        pricing_key = next((name for name in (row["Deployment"], row["Model"]) if name in pricing), "")
        rates = pricing.get(pricing_key)
        currency = rates.currency if rates is not None else "not configured"
        cost, state = _estimate(row, rates)
        priced = dict(row, PricingKey=pricing_key, Currency=currency,
                      PricingSource=rates.source if rates is not None else "",
                      EstimatedCost=cost, CostState=state)
        details.append(priced)
        key = (row["Interaction"], row["Deployment"], row["Model"], currency, pricing_key)
        summary = summaries.setdefault(key, {
            "Interaction": row["Interaction"], "Deployment": row["Deployment"], "Model": row["Model"],
            "Currency": currency, "PricingKey": pricing_key, "PricingSource": priced["PricingSource"],
            "ResponseRecords": 0, "UsageReported": 0, "UsageMissing": 0,
            "KnownInputTokens": 0, "KnownOutputTokens": 0, "KnownCachedInputTokens": 0,
            "KnownReasoningTokens": 0, "CachedUsageMissing": 0, "ReasoningUsageMissing": 0,
            "PricedResponses": 0, "UnpricedResponses": 0, "EstimatedCost": None,
        })
        summary["ResponseRecords"] += 1
        valid_usage = row["UsageState"] == "reported" and not _usage_problem(
            row["InputTokens"], row["OutputTokens"], row["CachedInputTokens"], row["ReasoningTokens"],
        )
        summary["UsageReported" if valid_usage else "UsageMissing"] += 1
        if valid_usage:
            for field in ("InputTokens", "OutputTokens", "CachedInputTokens", "ReasoningTokens"):
                value = row[field]
                if value is not None:
                    summary["Known" + field] += value
            summary["CachedUsageMissing"] += row["CachedInputTokens"] is None
            summary["ReasoningUsageMissing"] += row["ReasoningTokens"] is None
        summary["PricedResponses" if cost is not None else "UnpricedResponses"] += 1
        if cost is not None:
            previous = summary["EstimatedCost"]
            summary["EstimatedCost"] = cost if previous is None else previous + cost
    for summary in summaries.values():
        summary["EstimateCoverage"] = (
            "complete for reported responses" if not summary["UnpricedResponses"]
            else "partial; excludes unpriced responses" if summary["PricedResponses"]
            else "not estimated"
        )
    return list(summaries.values()), details
