"""
Web Search 工具扩展：为学习场景 Agent 提供项目外知识发现与正文抓取能力。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from learning_agent.ai import (
    AfterToolExecuteInput,
    AfterToolExecuteResult,
    BeforeToolExecuteInput,
    BeforeToolExecuteResult,
    HookDecision,
    HookName,
    ToolDefinition,
)
from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.learning_agent.providers import UnsupportedPageError
from learning_agent.learning_agent.providers.adapters import (
    BuiltinWebFetchProvider,
    BuiltinWebSearchProvider,
)
from learning_agent.learning_agent.services import (
    DEFAULT_ALLOWED_SOURCE_TYPES,
    WebSearchService,
    WebSearchServiceConfig,
    build_web_search_config,
    canonicalize_url,
)

logger = logging.getLogger(__name__)


@dataclass
class _TurnBudgetState:
    search_calls: int = 0
    fetch_calls: int = 0
    same_domain_streak: int = 0
    total_fetch_chars: int = 0
    failed_fetch_calls: int = 0
    last_domain: str | None = None
    authoritative_results_seen: bool = False
    evidence_ready: bool = False


def _turn_key(session_id: str, trace_id: str | None, turn_id: int | None) -> tuple[str, str]:
    if trace_id:
        return session_id, f"trace:{trace_id}"
    return session_id, f"turn:{int(turn_id or 0)}"


def _domain_for_budget(url: str) -> str:
    canonical = canonicalize_url(url)
    return urlparse(canonical or url).netloc.lower()


def _budget_snapshot(state: _TurnBudgetState, config: WebSearchServiceConfig) -> dict[str, int]:
    return {
        "search_calls": state.search_calls,
        "fetch_calls": state.fetch_calls,
        "failed_fetch_calls": state.failed_fetch_calls,
        "same_domain_streak": state.same_domain_streak,
        "total_fetch_chars": state.total_fetch_chars,
        "max_fetch_calls_per_turn": config.max_fetch_calls_per_turn,
        "max_same_domain_fetches_per_turn": config.max_same_domain_fetches_per_turn,
        "max_total_fetch_chars_per_turn": config.max_total_fetch_chars_per_turn,
    }


def _is_placeholder_url(value: str) -> bool:
    stripped = value.strip()
    if "{" in stripped or "}" in stripped:
        return True
    lowered = stripped.lower()
    placeholder_fragments = (
        "from web_search",
        "search result",
        "selected result",
        "从web_search",
        "从 web_search",
        "从搜索结果",
        "搜索结果中",
        "选择最权威",
        "官方页面url",
        "官方 url",
        "填入",
        "占位",
    )
    return any(fragment in lowered for fragment in placeholder_fragments)


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
    section_cursor: str | None = Field(default=None, description="Optional section cursor such as s0 or s1")
    limit_chars: int = Field(default=12000, description="Maximum characters to return")
    query: str | None = Field(
        default=None,
        description=(
            "Optional query: when provided, the page is scored at the section level and only the "
            "most relevant sections are returned (instead of paginating from the top). "
            "Use this when you want a specific concept/API on a long page."
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_concrete_http_url(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("URL cannot be empty.")
        if _is_placeholder_url(raw):
            raise ValueError(
                "web_fetch.url must be a concrete http/https URL, not a placeholder. "
                "Call web_search first, wait for its results, then retry web_fetch with an actual result URL."
            )
        if not canonicalize_url(raw):
            raise ValueError("Only concrete http/https URLs are supported.")
        return raw


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
        overall_timeout_seconds=web_config.fetch_overall_timeout_seconds,
        ca_bundle_path=web_config.tls_ca_bundle_path,
        prefer_system_trust_store=web_config.prefer_system_trust_store,
        trafilatura_enabled=web_config.web_fetch_trafilatura_enabled,
        jina_reader_enabled=web_config.web_fetch_jina_reader_enabled,
        jina_reader_base_url=web_config.web_fetch_jina_reader_base_url,
    )
    return WebSearchService(search_provider, fetch_provider, config=web_config)


def create_web_search_tools_extension(config: dict | None = None) -> Extension:
    ext = Extension(
        id="core-web-search-tools",
        name="Web Search Tools",
        version="0.4.0-alpha.1",
        type="builtin",
        config=config or {},
    )

    async def activate(ctx: ExtensionContext) -> None:
        web_config = (ctx.config or {}).get("web_search", {})
        if not web_config.get("enabled", True):
            logger.info("[core-web-search-tools] disabled by config")
            return

        service = _build_web_search_service(web_config)
        service_config = build_web_search_config(web_config)
        turn_budgets: dict[tuple[str, str], _TurnBudgetState] = {}

        def _get_state(session_id: str, trace_id: str | None, turn_id: int | None) -> _TurnBudgetState:
            key = _turn_key(session_id, trace_id, turn_id)
            state = turn_budgets.get(key)
            if state is None:
                state = _TurnBudgetState()
                turn_budgets[key] = state
            return state

        def _cleanup_session_turns(session_id: str, trace_id: str | None, turn_id: int | None) -> None:
            current_key = _turn_key(session_id, trace_id, turn_id)
            stale_keys = [
                key
                for key in turn_budgets
                if key[0] == session_id and key != current_key
            ]
            for key in stale_keys:
                turn_budgets.pop(key, None)

        async def _before_tool_execute(hook_input: BeforeToolExecuteInput) -> BeforeToolExecuteResult:
            if hook_input.tool_name not in {"web_search", "web_fetch"}:
                return BeforeToolExecuteResult()

            session_id = hook_input.context.session_id
            turn_id = hook_input.context.turn_id
            trace_id = hook_input.context.trace_id
            _cleanup_session_turns(session_id, trace_id, turn_id)
            state = _get_state(session_id, trace_id, turn_id)

            if state.evidence_ready:
                return BeforeToolExecuteResult(
                    decision=HookDecision.DENY,
                    deny_reason=(
                        "You already have enough authoritative evidence for this turn. "
                        "Answer now instead of calling more web tools."
                    ),
                    annotations={"budget_reason": "evidence_ready", **_budget_snapshot(state, service_config)},
                )

            if hook_input.tool_name == "web_search":
                if state.search_calls >= 2:
                    return BeforeToolExecuteResult(
                        decision=HookDecision.DENY,
                        deny_reason=(
                            "Web search budget reached for this turn. "
                            "Use the evidence you already found instead of starting another search."
                        ),
                        annotations={"budget_reason": "max_search_calls", **_budget_snapshot(state, service_config)},
                    )
                if state.authoritative_results_seen and state.search_calls >= 1 and state.fetch_calls == 0:
                    return BeforeToolExecuteResult(
                        decision=HookDecision.DENY,
                        deny_reason=(
                            "You already have authoritative search results for this turn. "
                            "Fetch the best current result before starting another search."
                        ),
                        annotations={"budget_reason": "authoritative_results_already_found", **_budget_snapshot(state, service_config)},
                    )
                if state.authoritative_results_seen and state.fetch_calls > 0 and state.failed_fetch_calls == 0:
                    return BeforeToolExecuteResult(
                        decision=HookDecision.DENY,
                        deny_reason=(
                            "You already searched authoritative sources and fetched evidence from them. "
                            "Answer now or continue only within the current authoritative page."
                        ),
                        annotations={"budget_reason": "authoritative_search_already_consumed", **_budget_snapshot(state, service_config)},
                    )
                return BeforeToolExecuteResult(annotations=_budget_snapshot(state, service_config))

            domain = _domain_for_budget(str(hook_input.arguments.get("url", "")).strip())
            explicit_continuation = bool(hook_input.arguments.get("section_cursor")) or int(
                hook_input.arguments.get("offset", 0) or 0
            ) > 0

            if state.fetch_calls >= service_config.max_fetch_calls_per_turn:
                return BeforeToolExecuteResult(
                    decision=HookDecision.DENY,
                    deny_reason=(
                        "Web fetch budget reached for this turn. "
                        "Answer with the evidence you already have unless a very specific detail is still missing."
                    ),
                    annotations={"budget_reason": "max_fetch_calls", **_budget_snapshot(state, service_config)},
                )

            if (
                domain
                and state.last_domain == domain
                and state.same_domain_streak >= service_config.max_same_domain_fetches_per_turn
                and not explicit_continuation
            ):
                return BeforeToolExecuteResult(
                    decision=HookDecision.DENY,
                    deny_reason=(
                        "Repeated web fetches from the same domain have reached the soft limit for this turn. "
                        "Only continue if you need one specific missing detail from an explicit section."
                    ),
                    annotations={"budget_reason": "same_domain_streak", **_budget_snapshot(state, service_config)},
                )

            remaining_chars = service_config.max_total_fetch_chars_per_turn - state.total_fetch_chars
            if remaining_chars <= 0:
                return BeforeToolExecuteResult(
                    decision=HookDecision.DENY,
                    deny_reason=(
                        "Web fetch character budget reached for this turn. "
                        "Answer using current evidence instead of reading more."
                    ),
                    annotations={"budget_reason": "max_total_fetch_chars", **_budget_snapshot(state, service_config)},
                )

            patched_arguments: dict[str, Any] = {}
            requested_limit = int(hook_input.arguments.get("limit_chars", service_config.default_limit_chars) or 0)
            if requested_limit > remaining_chars:
                if remaining_chars < 1000:
                    return BeforeToolExecuteResult(
                        decision=HookDecision.DENY,
                        deny_reason=(
                            "Remaining web fetch budget is too small for another useful read. "
                            "Answer now unless one exact missing detail still matters."
                        ),
                        annotations={"budget_reason": "remaining_chars_too_small", **_budget_snapshot(state, service_config)},
                    )
                patched_arguments["limit_chars"] = remaining_chars

            return BeforeToolExecuteResult(
                patched_arguments=patched_arguments,
                annotations={"budget_domain": domain, **_budget_snapshot(state, service_config)},
            )

        async def _after_tool_execute(hook_input: AfterToolExecuteInput) -> AfterToolExecuteResult:
            if hook_input.tool_name not in {"web_search", "web_fetch"}:
                return AfterToolExecuteResult()

            session_id = hook_input.context.session_id
            turn_id = hook_input.context.turn_id
            trace_id = hook_input.context.trace_id
            _cleanup_session_turns(session_id, trace_id, turn_id)
            state = _get_state(session_id, trace_id, turn_id)

            if hook_input.tool_name == "web_search":
                if hook_input.success and isinstance(hook_input.result, dict):
                    results = hook_input.result.get("results") or []
                    state.authoritative_results_seen = state.authoritative_results_seen or any(
                        item.get("source_type") in {"official_docs", "official_blog", "paper", "github_repo"}
                        for item in results
                        if isinstance(item, dict)
                    )
                state.search_calls += 1
                return AfterToolExecuteResult(
                    extra_metadata={
                        "authoritative_results_seen": state.authoritative_results_seen,
                        "evidence_ready": int(bool(state.evidence_ready)),
                        **_budget_snapshot(state, service_config),
                    }
                )

            if hook_input.tool_name == "web_fetch":
                result_payload = hook_input.result if isinstance(hook_input.result, dict) else {}
                fetched_chars = len(str(result_payload.get("content", ""))) if hook_input.success else 0
                domain = str(result_payload.get("domain") or _domain_for_budget(str(hook_input.arguments.get("url", ""))))
                guidance = result_payload.get("guidance") if isinstance(result_payload, dict) else {}
                state.fetch_calls += 1
                state.total_fetch_chars += fetched_chars
                if not hook_input.success:
                    state.failed_fetch_calls += 1
                if domain and state.last_domain == domain:
                    state.same_domain_streak += 1
                else:
                    state.same_domain_streak = 1 if domain else 0
                state.last_domain = domain or state.last_domain
                if hook_input.success and isinstance(guidance, dict) and guidance.get("should_stop_after_this") is True:
                    state.evidence_ready = True
                return AfterToolExecuteResult(
                    extra_metadata={
                        "fetched_chars": fetched_chars,
                        "domain": domain,
                        "fetch_succeeded": int(bool(hook_input.success)),
                        "evidence_ready": int(bool(state.evidence_ready)),
                        **_budget_snapshot(state, service_config),
                    }
                )

            return AfterToolExecuteResult(extra_metadata=_budget_snapshot(state, service_config))

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
            section_cursor: str | None = None,
            limit_chars: int = 12000,
            query: str | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            del kwargs
            if not str(url or "").strip():
                return _tool_error("INVALID_URL", "URL cannot be empty.")
            try:
                return await service.fetch(
                    url=url,
                    offset=offset,
                    section_cursor=section_cursor,
                    limit_chars=limit_chars,
                    query=query,
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
                    "the answer depends on official docs, release notes, or community discussions. "
                    "Prefer one authoritative result over many weak ones, and stop searching once you already have sufficient evidence."
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
                    "Fetch readable content from a web page. Returns cleaned text plus structured headings, "
                    "section paths, code blocks, and supports section-based continued reading. "
                    "Provide `query` to get only the most relevant sections of long pages (recommended). "
                    "Stop after you have enough evidence, and only continue if you still need one specific missing detail."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target page URL"},
                        "offset": {"type": "integer", "description": "Character offset for continued reading", "default": 0},
                        "section_cursor": {
                            "type": "string",
                            "description": "Optional section cursor such as s0 or s1 for heading-level continuation",
                        },
                        "limit_chars": {"type": "integer", "description": "Maximum characters to return", "default": 12000},
                        "query": {
                            "type": "string",
                            "description": (
                                "Optional: when provided, the page is scored at the section level and only the "
                                "most relevant sections are returned (instead of paginating from the top). "
                                "Use this for long pages where you want a specific concept/API."
                            ),
                        },
                    },
                    "required": ["url"],
                },
                input_model=WebFetchInput,
            ),
            _tool_web_fetch,
        )

        ctx.register_hook(
            HookName.BEFORE_TOOL_EXECUTE,
            _before_tool_execute,
            priority=120,
        )
        ctx.register_hook(
            HookName.AFTER_TOOL_EXECUTE,
            _after_tool_execute,
            priority=120,
        )

    ext.on_activate(activate)
    return ext


__all__ = ["create_web_search_tools_extension"]
