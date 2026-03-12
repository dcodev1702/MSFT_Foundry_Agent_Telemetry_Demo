from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable


McpQuery = Callable[[str], Awaitable[Any]]


class MicrosoftLearnMcpService:
    """Query Microsoft Learn through an MCP server configured for the bot runtime."""

    DEFAULT_TOOL_CANDIDATES = (
        "microsoft_docs_search",
        "msft_docs_search",
        "docs_search",
    )

    def __init__(self, query_tool: McpQuery | None = None):
        self._query_tool = query_tool or self._query_tool_via_mcp

    async def get_docs_text(self, question: str | None) -> str:
        query = (question or "").strip()
        if not query:
            return "Usage: `msft_docs <question>`"

        try:
            payload = await self._query_tool(query)
        except Exception as exc:
            return f"Microsoft Learn lookup failed for `{query}`: {exc}"

        return self._format_payload(query, payload)

    async def _query_tool_via_mcp(self, query: str) -> Any:
        server_url = os.getenv("MSFT_LEARN_MCP_URL", "https://learn.microsoft.com/api/mcp").strip()
        if not server_url:
            raise RuntimeError("Set MSFT_LEARN_MCP_URL to the Microsoft Learn MCP endpoint.")

        timeout_seconds = float(os.getenv("MSFT_LEARN_MCP_TIMEOUT_SECONDS", "20"))

        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except Exception as exc:
            raise RuntimeError(
                "The `mcp` Python package is required for `msft_docs`. Install the updated requirements."
            ) from exc

        try:
            import httpx
        except Exception as exc:
            raise RuntimeError(
                "The `httpx` Python package is required for HTTP MCP access. Install the updated requirements."
            ) from exc

        async def _run_query() -> Any:
            http_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds),
                headers={
                    "User-Agent": "bot-the-builder/1.0",
                },
            )
            async with http_client:
                async with streamable_http_client(server_url, http_client=http_client) as (read, write, _get_session_id):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        tool_name = self._resolve_tool_name(tools)
                        result = await session.call_tool(tool_name, arguments={"query": query})
                        return self._normalize_tool_result(result)

        return await asyncio.wait_for(_run_query(), timeout=timeout_seconds)

    def _resolve_tool_name(self, tools_response: Any) -> str:
        configured = os.getenv("MSFT_LEARN_MCP_TOOL_NAME", "").strip()
        available_tools = [tool.name for tool in getattr(tools_response, "tools", [])]

        if configured:
            if configured in available_tools:
                return configured
            raise RuntimeError(
                f"Configured Microsoft Learn MCP tool '{configured}' was not found. Available tools: {', '.join(available_tools) or 'none'}"
            )

        for candidate in self.DEFAULT_TOOL_CANDIDATES:
            if candidate in available_tools:
                return candidate

        raise RuntimeError(
            "No supported Microsoft Learn MCP search tool was found. "
            f"Available tools: {', '.join(available_tools) or 'none'}"
        )

    @staticmethod
    def _normalize_tool_result(result: Any) -> Any:
        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured

        content = getattr(result, "content", None) or []
        text_blocks: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                text_blocks.append(text)

        if len(text_blocks) == 1:
            single = text_blocks[0]
            try:
                return json.loads(single)
            except json.JSONDecodeError:
                return single

        if text_blocks:
            return text_blocks

        return "No content returned from Microsoft Learn MCP."

    def _format_payload(self, query: str, payload: Any) -> str:
        if isinstance(payload, str):
            return self._format_text_payload(query, payload)

        if isinstance(payload, list):
            return self._format_result_list(query, payload)

        if isinstance(payload, dict):
            if "results" in payload and isinstance(payload["results"], list):
                return self._format_result_list(query, payload["results"])
            if "value" in payload and isinstance(payload["value"], list):
                return self._format_result_list(query, payload["value"])
            return self._format_result_list(query, [payload])

        return f"Microsoft Learn returned an unsupported payload for `{query}`."

    def _format_result_list(self, query: str, results: list[Any]) -> str:
        normalized_results = [
            result for result in results
            if isinstance(result, dict)
        ]
        if not normalized_results:
            return f"No Microsoft Learn results found for `{query}`."

        lines = [f"📚 Microsoft Learn results for `{query}`"]
        for index, result in enumerate(normalized_results[:3], start=1):
            title = result.get("title") or result.get("name") or f"Result {index}"
            url = result.get("url") or result.get("link") or result.get("uri")
            excerpt = (
                result.get("content")
                or result.get("excerpt")
                or result.get("snippet")
                or result.get("description")
            )

            lines.append(f"{index}. {title}")
            if url:
                lines.append(str(url))
            if excerpt:
                excerpt_text = str(excerpt).replace("\n", " ").strip()
                if len(excerpt_text) > 320:
                    excerpt_text = excerpt_text[:317].rstrip() + "..."
                lines.append(excerpt_text)

        return "<br>".join(lines)

    @staticmethod
    def _format_text_payload(query: str, payload: str) -> str:
        text = payload.strip()
        if not text:
            return f"No Microsoft Learn results found for `{query}`."
        return "<br>".join([
            f"📚 Microsoft Learn result for `{query}`",
            text,
        ])
