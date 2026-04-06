from __future__ import annotations

import logging
import os
from typing import Annotated, Any, Awaitable, Callable

from models import ALLOWED_MODELS
from msft_docs_service import MicrosoftLearnMcpService


logger = logging.getLogger(__name__)

AgentRunner = Callable[..., Awaitable[str]]


class AgentFrameworkCommandOrchestrator:
    """Optional Agent Framework layer behind the Teams bot handlers."""

    def __init__(
        self,
        msft_docs_service: MicrosoftLearnMcpService,
        *,
        enabled: bool | None = None,
        allowed_models: list[str] | None = None,
        agent_runner: AgentRunner | None = None,
    ):
        self._msft_docs_service = msft_docs_service
        self._enabled = self._resolve_enabled(enabled)
        self._allowed_models = tuple(allowed_models or ALLOWED_MODELS)
        self._agent_runner = agent_runner or self._run_live_agent

    async def get_msft_docs_text(self, query: str | None) -> str:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return await self._msft_docs_service.get_docs_text(query)

        if not self._enabled:
            return await self._msft_docs_service.get_docs_text(normalized_query)

        try:
            return await self._agent_runner(
                name="MicrosoftLearnCommandAgent",
                instructions=(
                    "You are the internal orchestration agent for a Microsoft Teams bot. "
                    "Use the provided tool to search Microsoft Learn and answer only with grounded information. "
                    "Be concise, operational, and do not invent URLs or commands."
                ),
                prompt=normalized_query,
                tools=[self._create_docs_tool()],
            )
        except Exception as exc:
            logger.warning(
                "Falling back to direct Microsoft Learn MCP lookup after Agent Framework failure: %s",
                exc,
            )
            return await self._msft_docs_service.get_docs_text(normalized_query)

    async def get_build_guidance(
        self,
        raw_command: str,
        *,
        invalid_model: str | None = None,
    ) -> str:
        if not self._enabled:
            return self._default_build_guidance(invalid_model)

        try:
            prompt_lines = [
                f"User command: {raw_command or 'build it'}",
                "Explain how to continue without claiming that a deployment has started.",
                "Keep the response to 6 lines or fewer.",
            ]
            if invalid_model:
                prompt_lines.append(f"The model '{invalid_model}' is not supported.")

            return await self._agent_runner(
                name="FoundryBuildGuidanceAgent",
                instructions=(
                    "You explain the Teams bot build flow for Azure AI Foundry. "
                    "You may only describe the supported models and the next user action. "
                    "Never say a build was queued, started, or approved unless the caller already confirmed it."
                ),
                prompt="\n".join(prompt_lines),
                tools=[self._create_supported_models_tool()],
            )
        except Exception as exc:
            logger.warning(
                "Falling back to deterministic build guidance after Agent Framework failure: %s",
                exc,
            )
            return self._default_build_guidance(invalid_model)

    def _default_build_guidance(self, invalid_model: str | None = None) -> str:
        lines = []
        if invalid_model:
            lines.append(f"Unknown model `{invalid_model}`.")
        else:
            lines.append("**Select a model for your build:**")

        if invalid_model:
            lines.append("")
            lines.append("**Select a model for your build:**")

        lines.append("")
        for index, model in enumerate(self._allowed_models, start=1):
            lines.append(f"{index}. `{model}`")
        lines.append("")
        lines.append("Reply with a **number** or the **model name**.")
        lines.append("Type `cancel` to abort.")
        return "<br>".join(lines)

    def _create_docs_tool(self):
        async def search_microsoft_learn(
            search_query: Annotated[str, "The Microsoft Learn question to answer"],
        ) -> str:
            """Search Microsoft Learn through the configured MCP client."""
            return await self._msft_docs_service.get_docs_text(search_query)

        return search_microsoft_learn

    def _create_supported_models_tool(self):
        async def get_supported_models() -> str:
            """Return the supported Azure AI Foundry deployment models for the Teams bot build flow."""
            return "\n".join(self._allowed_models)

        return get_supported_models

    async def _run_live_agent(
        self,
        *,
        name: str,
        instructions: str,
        prompt: str,
        tools: list[Callable[..., Any]],
    ) -> str:
        config = self._load_live_config()

        from agent_framework.openai import OpenAIChatClient
        from azure.identity import DefaultAzureCredential

        managed_identity_client_id = os.getenv("AZURE_CLIENT_ID") or None
        credential = DefaultAzureCredential(managed_identity_client_id=managed_identity_client_id)

        client_kwargs: dict[str, Any] = {
            "model": config["deployment_name"],
            "azure_endpoint": config["endpoint"],
            "credential": credential,
        }
        if config["api_version"]:
            client_kwargs["api_version"] = config["api_version"]

        agent = OpenAIChatClient(**client_kwargs).as_agent(
            name=name,
            instructions=instructions,
            tools=tools,
        )
        result = await agent.run(prompt)
        text = self._coerce_text(result)
        if not text:
            raise RuntimeError("Agent Framework returned an empty response.")
        return text.replace("\r\n", "\n").replace("\n", "<br>")

    @staticmethod
    def _coerce_text(result: Any) -> str:
        if isinstance(result, str):
            return result.strip()

        for attr in ("output_text", "text", "content"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        return str(result).strip()

    @staticmethod
    def _resolve_enabled(enabled: bool | None) -> bool:
        if enabled is not None:
            return enabled
        return os.getenv("BOT_AGENT_FRAMEWORK_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _load_live_config() -> dict[str, str]:
        endpoint = (
            os.getenv("BOT_AGENT_FRAMEWORK_ENDPOINT", "").strip()
            or os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
            or os.getenv("WEATHER_LLM_AZURE_OPENAI_ENDPOINT", "").strip()
        )
        deployment_name = (
            os.getenv("BOT_AGENT_FRAMEWORK_DEPLOYMENT_NAME", "").strip()
            or os.getenv("AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME", "").strip()
            or os.getenv("WEATHER_LLM_MODEL", "").strip()
        )
        api_version = (
            os.getenv("BOT_AGENT_FRAMEWORK_API_VERSION", "").strip()
            or os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
            or os.getenv("WEATHER_LLM_API_VERSION", "").strip()
        )

        if not endpoint:
            raise RuntimeError(
                "Set BOT_AGENT_FRAMEWORK_ENDPOINT, AZURE_OPENAI_ENDPOINT, or WEATHER_LLM_AZURE_OPENAI_ENDPOINT to enable Agent Framework orchestration."
            )
        if not deployment_name:
            raise RuntimeError(
                "Set BOT_AGENT_FRAMEWORK_DEPLOYMENT_NAME, AZURE_OPENAI_RESPONSES_DEPLOYMENT_NAME, or WEATHER_LLM_MODEL to enable Agent Framework orchestration."
            )

        return {
            "endpoint": endpoint,
            "deployment_name": deployment_name,
            "api_version": api_version,
        }