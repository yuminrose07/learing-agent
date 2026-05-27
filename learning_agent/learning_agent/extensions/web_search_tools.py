"""
Web Search 工具扩展：为学习场景 Agent 提供项目外知识发现与正文抓取能力。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from learning_agent.ai import ToolDefinition
from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.learning_agent.providers import UnsupportedPageError
from learning_agent.learning_agent.providers.adapters import (
    BuiltinWebFetchProvider,
    BuiltinWebSearchProvider,
)
from learning_agent.learning_agent.services import (
    DEFAULT_ALLOWED_SOURCE_TYPES,
    WebSearchService,
    build_web_search_config,
)

logger = logging.getLogger(__name__)


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query")
    top_k: int = Field(default=5, description="Maximum number of results to return")
    freshness: str = Field(default="any", description="Freshness hint: any | month | year")
    source_preferences: list[str] = Field(
        default_factory=list,
        description="Preferred source types such as official_docs, github_repo, blog",
    )
    language: str = Field(default="any", description="Preferred result language: zh | en | any")


class WebFetchInput(BaseModel):
    url: str = Field(description="Target page URL")
    offset: int = Field(default=0, description="Character offset for continued reading")
    limit_chars: int = Field(default=12000, description="Maximum characters to return")


def _tool_error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
    }


def _build_web_search_service(config: Optional[dict[str, Any]]) -> WebSearchService:
    web_config = build_web_search_config(config)
    provider_name = web_config.provider_name
    if provider_name != "builtin":
        raise ValueError(f"Unsupported web_search provider: {provider_name}")
    search_provider = BuiltinWebSearchProvider(
        timeout_seconds=web_config.timeout_seconds,
        ca_bundle_path=web_config.tls_ca_bundle_path,
        prefer_system_trust_store=web_config.prefer_system_trust_store,
    )
    fetch_provider = BuiltinWebFetchProvider(
        timeout_seconds=web_config.timeout_seconds,
        ca_bundle_path=web_config.tls_ca_bundle_path,
        prefer_system_trust_store=web_config.prefer_system_trust_store,
    )
    return WebSearchService(search_provider, fetch_provider, config=web_config)


def create_web_search_tools_extension(config: dict | None = None) -> Extension:
    ext = Extension(
        id="core-web-search-tools",
        name="Web Search Tools",
        version="0.1.0",
        type="builtin",
        config=config or {},
    )

    async def activate(ctx: ExtensionContext) -> None:
        web_config = (ctx.config or {}).get("web_search", {})
        if not web_config.get("enabled", True):
            logger.info("[core-web-search-tools] disabled by config")
            return

        service = _build_web_search_service(web_config)

        async def _tool_web_search(
            query: str,
            top_k: int = 5,
            freshness: str = "any",
            source_preferences: Optional[list[str]] = None,
            language: str = "any",
            **kwargs: Any,
        ) -> dict[str, Any]:
            del kwargs
            if not str(query or "").strip():
                return _tool_error("INVALID_QUERY", "Search query cannot be empty.")
            try:
                return await service.search(
                    query=query,
                    top_k=top_k,
                    freshness=freshness,
                    source_preferences=source_preferences or [],
                    language=language,
                )
            except ValueError as exc:
                return _tool_error("INVALID_QUERY", str(exc))

        async def _tool_web_fetch(
            url: str,
            offset: int = 0,
            limit_chars: int = 12000,
            **kwargs: Any,
        ) -> dict[str, Any]:
            del kwargs
            if not str(url or "").strip():
                return _tool_error("INVALID_URL", "URL cannot be empty.")
            try:
                return await service.fetch(
                    url=url,
                    offset=offset,
                    limit_chars=limit_chars,
                )
            except ValueError as exc:
                return _tool_error("INVALID_URL", str(exc))
            except UnsupportedPageError as exc:
                return _tool_error("UNSUPPORTED_PAGE", str(exc))

        ctx.register_tool(
            ToolDefinition(
                id="web_search",
                name="web_search",
                description=(
                    "Search the web for external knowledge sources. Returns a small set of structured "
                    "results with title, URL, snippet, domain, source type, and score. Use this when "
                    "the answer depends on official docs, release notes, or community discussions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "top_k": {"type": "integer", "description": "Maximum number of results to return", "default": 5},
                        "freshness": {"type": "string", "description": "Freshness hint: any | month | year", "default": "any"},
                        "source_preferences": {
                            "type": "array",
                            "items": {"type": "string", "enum": DEFAULT_ALLOWED_SOURCE_TYPES},
                            "description": "Preferred source types",
                            "default": [],
                        },
                        "language": {"type": "string", "description": "Preferred result language: zh | en | any", "default": "any"},
                    },
                    "required": ["query"],
                },
                input_model=WebSearchInput,
            ),
            _tool_web_search,
        )

        ctx.register_tool(
            ToolDefinition(
                id="web_fetch",
                name="web_fetch",
                description=(
                    "Fetch readable content from a web page. Returns cleaned text instead of raw HTML, "
                    "and supports continued reading via offset and limit_chars."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target page URL"},
                        "offset": {"type": "integer", "description": "Character offset for continued reading", "default": 0},
                        "limit_chars": {"type": "integer", "description": "Maximum characters to return", "default": 12000},
                    },
                    "required": ["url"],
                },
                input_model=WebFetchInput,
            ),
            _tool_web_fetch,
        )

    ext.on_activate(activate)
    return ext


__all__ = ["create_web_search_tools_extension"]
