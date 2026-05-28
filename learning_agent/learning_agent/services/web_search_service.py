from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from learning_agent.learning_agent.providers import (
    RawFetchedPage,
    RawSearchResult,
    UnsupportedPageError,
    WebFetchProvider,
    WebSearchProvider,
)

DEFAULT_ALLOWED_SOURCE_TYPES = [
    "official_docs",
    "official_blog",
    "github_repo",
    "github_issue",
    "community_forum",
    "blog",
    "paper",
    "news",
    "aggregator",
]

_TRACKING_QUERY_KEYS = {
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
    "ref",
    "ref_src",
    "source",
}
_TRUST_SCORES = {
    "official_docs": 1.0,
    "official_blog": 0.92,
    "github_repo": 0.88,
    "github_issue": 0.82,
    "community_forum": 0.76,
    "paper": 0.8,
    "blog": 0.6,
    "news": 0.55,
    "aggregator": 0.25,
}
_OFFICIAL_DOC_DOMAINS = {
    "developer.mozilla.org",
    "docs.anthropic.com",
    "docs.github.com",
    "docs.gitlab.com",
    "docs.pydantic.dev",
    "docs.python.org",
    "docs.stripe.com",
    "docs.aws.amazon.com",
    "fastapi.tiangolo.com",
    "platform.openai.com",
    "react.dev",
    "starlette.dev",
    "vite.dev",
}
_OFFICIAL_DOC_DOMAIN_SUFFIXES = (
    ".readthedocs.io",
)
_OFFICIAL_DOC_PATH_MARKERS = (
    "/api",
    "/api-reference",
    "/docs",
    "/documentation",
    "/guide",
    "/guides",
    "/manual",
    "/reference",
    "/references",
    "/tutorial",
    "/tutorials",
)
_OFFICIAL_BLOG_DOMAINS = {
    "blog.cloudflare.com",
    "netflixtechblog.com",
}
_OFFICIAL_BLOG_PATH_MARKERS = (
    "/blog",
    "/changelog",
    "/engineering",
    "/index",
    "/news",
    "/updates",
)
_PAPER_DOMAINS = {
    "aclanthology.org",
    "arxiv.org",
    "dl.acm.org",
    "ieeexplore.ieee.org",
    "openreview.net",
    "papers.nips.cc",
    "proceedings.mlr.press",
}
_PAPER_DOMAIN_SUFFIXES = (
    ".arxiv.org",
    ".semanticscholar.org",
)
_PAPER_PATH_MARKERS = (
    "/abs/",
    "/pdf/",
    "/doi/",
    "/paper/",
    "/papers/",
)
_GENERIC_SECTION_HEADINGS = {
    "api",
    "api reference",
    "docs",
    "documentation",
    "example",
    "examples",
    "getting started",
    "guide",
    "guides",
    "index",
    "introduction",
    "intro",
    "overview",
    "reference",
    "response",
    "responses",
    "tutorial",
    "tutorials",
}


@dataclass(slots=True)
class WebSearchServiceConfig:
    provider_name: str = "builtin"
    default_top_k: int = 5
    max_top_k: int = 10
    default_limit_chars: int = 12000
    max_fetch_calls_per_turn: int = 3
    max_same_domain_fetches_per_turn: int = 2
    max_total_fetch_chars_per_turn: int = 24000
    timeout_seconds: float = 12.0
    fetch_overall_timeout_seconds: float = 30.0
    tls_ca_bundle_path: str | None = None
    prefer_system_trust_store: bool = True
    web_fetch_trafilatura_enabled: bool = True
    web_fetch_jina_reader_enabled: bool = False
    web_fetch_jina_reader_base_url: str = "https://r.jina.ai"
    allowed_source_types: tuple[str, ...] = tuple(DEFAULT_ALLOWED_SOURCE_TYPES)


def build_web_search_config(values: Optional[dict]) -> WebSearchServiceConfig:
    values = values or {}
    allowed = values.get("allowed_source_types") or DEFAULT_ALLOWED_SOURCE_TYPES
    return WebSearchServiceConfig(
        provider_name=str(values.get("provider", "builtin")),
        default_top_k=max(1, int(values.get("default_top_k", 5))),
        max_top_k=max(1, int(values.get("max_top_k", 10))),
        default_limit_chars=max(1000, int(values.get("default_limit_chars", 12000))),
        max_fetch_calls_per_turn=max(1, int(values.get("max_fetch_calls_per_turn", 3))),
        max_same_domain_fetches_per_turn=max(1, int(values.get("max_same_domain_fetches_per_turn", 2))),
        max_total_fetch_chars_per_turn=max(1000, int(values.get("max_total_fetch_chars_per_turn", 24000))),
        timeout_seconds=float(values.get("timeout_seconds", 12.0)),
        fetch_overall_timeout_seconds=float(values.get("fetch_overall_timeout_seconds", 30.0)),
        tls_ca_bundle_path=(
            str(values.get("tls_ca_bundle_path")).strip()
            if values.get("tls_ca_bundle_path")
            else None
        ),
        prefer_system_trust_store=bool(values.get("prefer_system_trust_store", True)),
        web_fetch_trafilatura_enabled=bool(values.get("web_fetch_trafilatura_enabled", True)),
        web_fetch_jina_reader_enabled=bool(values.get("web_fetch_jina_reader_enabled", False)),
        web_fetch_jina_reader_base_url=str(
            values.get("web_fetch_jina_reader_base_url") or "https://r.jina.ai"
        ),
        allowed_source_types=tuple(str(item) for item in allowed),
    )


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_QUERY_KEYS
    ]
    normalized = parsed._replace(
        fragment="",
        query=urlencode(clean_query, doseq=True),
        netloc=parsed.netloc.lower(),
    )
    return urlunparse(normalized)


def domain_from_url(url: str) -> str:
    return urlparse(url).netloc.lower()


def _normalize_domain(domain: str) -> str:
    domain = domain.lower().strip()
    if domain.startswith("www."):
        return domain[4:]
    return domain


def _matches_exact_or_suffix(domain: str, exact_domains: set[str], suffixes: tuple[str, ...]) -> bool:
    return domain in exact_domains or any(domain.endswith(suffix) for suffix in suffixes)


def _path_has_marker(path: str, markers: tuple[str, ...]) -> bool:
    return any(path == marker or path.startswith(f"{marker}/") or marker in path for marker in markers)


def _query_terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[a-z0-9_./+-]{2,}", query.lower()) if term not in {"site", "http", "https"}]


def _query_terms_for_section_match(query: str) -> list[str]:
    """块级匹配用的 query 词项:在 _query_terms 的 ASCII 词基础上补 CJK token。

    Why: _query_terms 只抽 ASCII,中文 query("任务组")直接返回空,
    会让块级打分全失效。把空格切分后长度 ≥2 的非 ASCII 段也作为 term 加入,
    让中文 query 也能在块级命中。
    """
    terms = list(_query_terms(query))
    for raw_token in (query or "").split():
        token = raw_token.strip().lower()
        if len(token) >= 2 and not token.isascii() and token not in terms:
            terms.append(token)
    return terms


def _degrade_query(query: str) -> str | None:
    """生成一次"更短的兜底 query"。

    退化策略：保留信息量最大的前 N 个 token。
    - 多 token query：取前 4 个 token，用空格拼接
    - 单 token 长 query：截前 8 个字符
    - 太短的 query（≤4 字符或仅 1-2 个 token 且总长 < 10）不退化
    """
    if not query:
        return None
    tokens = query.split()
    if len(tokens) > 4:
        return " ".join(tokens[:4])
    if len(tokens) == 1 and len(query) > 8:
        return query[:8]
    if len(tokens) == 2 and len(query) > 12:
        return tokens[0]
    return None


def _build_query_aware_excerpt(query: str, snippet: str, title: str) -> str:
    normalized_snippet = " ".join((snippet or "").split())
    if not normalized_snippet:
        return " ".join((title or "").split())

    terms = _query_terms(query)
    if not terms:
        return normalized_snippet

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？.!?])\s+|\n+", normalized_snippet)
        if part.strip()
    ]
    if not sentences:
        sentences = [normalized_snippet]

    scored: list[tuple[int, int, str]] = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        matched_terms = [term for term in terms if term in lowered]
        specificity_score = sum(len(term) for term in matched_terms)
        density_bonus = min(3, sum(lowered.count(term) for term in matched_terms))
        scored.append((specificity_score * 10 + density_bonus, -index, sentence))
    scored.sort(reverse=True)
    best_score, _, best_sentence = scored[0]
    if best_score <= 0:
        return normalized_snippet

    return best_sentence


def _score_section_against_query(query: str, section_content: str, section_heading: str) -> int:
    """块级相关度打分:heading 命中权重 ×3,content 命中带密度上限。

    沿用 _build_query_aware_excerpt 的 term-specificity 思路升级到块级 —— 长 term
    更稀有权重高,heading 命中比正文重要(标题是结构化提示),content 内同 term
    重复 5 次以上不再加分(避免单段被关键词刷屏占满 limit)。
    """
    terms = _query_terms_for_section_match(query)
    if not terms:
        return 0
    content_lower = (section_content or "").lower()
    heading_lower = (section_heading or "").lower()
    score = 0
    for term in terms:
        h_count = heading_lower.count(term)
        c_count = content_lower.count(term)
        if h_count + c_count == 0:
            continue
        specificity = len(term)
        score += specificity * (h_count * 3 + min(5, c_count))
    return score


def _parse_section_cursor(section_cursor: str, total_sections: int) -> int:
    cursor = str(section_cursor or "").strip().lower()
    if not cursor:
        raise ValueError("section_cursor cannot be empty.")
    if cursor.startswith("s"):
        cursor = cursor[1:]
    if not cursor.isdigit():
        raise ValueError("section_cursor must look like s0, s1, ...")
    index = int(cursor)
    if index < 0 or index >= total_sections:
        raise ValueError("section_cursor is out of range.")
    return index


def _section_summaries(page) -> list[dict]:
    return [
        {
            "cursor": section.cursor,
            "heading": section.heading,
            "section_path": list(section.section_path),
        }
        for section in page.sections
    ]


def _budget_policy(config: WebSearchServiceConfig) -> dict[str, int]:
    return {
        "max_fetch_calls_per_turn": config.max_fetch_calls_per_turn,
        "max_same_domain_fetches_per_turn": config.max_same_domain_fetches_per_turn,
        "max_total_fetch_chars_per_turn": config.max_total_fetch_chars_per_turn,
    }


def _search_guidance(results: list[dict], config: WebSearchServiceConfig) -> dict:
    authoritative_types = {"official_docs", "official_blog", "paper", "github_repo"}
    top_result = results[0] if results else None
    authoritative_results = [item for item in results if item.get("source_type") in authoritative_types]
    if top_result and top_result.get("source_type") in {"official_docs", "paper"}:
        recommended = "fetch_top_authoritative_result_then_answer"
    elif authoritative_results:
        recommended = "fetch_best_authoritative_result"
    elif top_result:
        recommended = "fetch_top_result_only_if_external_evidence_is_required"
    else:
        recommended = "retry_search_or_answer_from_existing_knowledge"
    return {
        "recommended_next_action": recommended,
        "stop_if": [
            "Top result is already an authoritative source and its snippet directly answers the question.",
            "You already have one authoritative source and do not need extra corroboration.",
        ],
        "continue_only_if": [
            "The snippet is insufficient to answer the question.",
            "You still need an API definition, exact example, or parameter detail.",
            "The current top result is not authoritative enough.",
        ],
        "budget_policy": _budget_policy(config),
    }


def _fetch_guidance(
    *,
    source_type: str,
    pagination_mode: str,
    truncated: bool,
    code_blocks: list[str],
    next_section_cursor: str | None,
    section_path: list[str],
    config: WebSearchServiceConfig,
) -> dict:
    authoritative = source_type in {"official_docs", "official_blog", "paper", "github_repo"}
    normalized_section_path = [item.strip() for item in section_path if item and item.strip()]
    current_heading = normalized_section_path[-1].lower() if normalized_section_path else ""
    is_specific_section = bool(current_heading) and current_heading not in _GENERIC_SECTION_HEADINGS
    has_nested_section_path = len(normalized_section_path) > 1
    section_has_enough_detail = bool(code_blocks) or not truncated or next_section_cursor is None
    # query 模式已经按相关度精选段,默认鼓励 answer_now —— 模型再翻页等于浪费 budget
    if pagination_mode == "query":
        should_stop = True
    else:
        should_stop = authoritative and (
            (pagination_mode == "offset" and not truncated)
            or (
                pagination_mode == "section"
                and section_has_enough_detail
                and (is_specific_section or has_nested_section_path)
            )
        )
    if should_stop:
        recommended = "answer_now"
    elif authoritative and pagination_mode == "section" and next_section_cursor:
        recommended = "continue_to_one_more_specific_section_only_if_missing_detail"
    elif next_section_cursor:
        recommended = "continue_only_for_a_specific_missing_detail"
    elif not authoritative:
        recommended = "prefer_a_more_authoritative_source_before_fetching_more"
    else:
        recommended = "answer_or_fetch_one_specific_missing_detail"
    return {
        "should_stop_after_this": should_stop,
        "recommended_next_action": recommended,
        "stop_if": [
            "This section already provides the needed definition, example, or answer.",
            "You already have an authoritative source and enough detail to respond.",
        ],
        "continue_only_if": [
            "The answer still lacks a specific API definition.",
            "The answer still lacks a concrete example or code snippet.",
            "The current page is not authoritative enough.",
            "A relevant next section is explicitly suggested.",
        ],
        "budget_policy": _budget_policy(config),
    }


def classify_source(url: str) -> str:
    parsed = urlparse(url)
    domain = _normalize_domain(parsed.netloc)
    path = parsed.path.lower()

    if domain in {"github.com", "raw.githubusercontent.com", "gist.githubusercontent.com"}:
        if "/issues/" in path or "/discussions/" in path:
            return "github_issue"
        return "github_repo"
    if any(host in domain for host in ("stackoverflow.com", "stackexchange.com", "reddit.com", "discuss.", "forum.")):
        return "community_forum"
    if _matches_exact_or_suffix(domain, _PAPER_DOMAINS, _PAPER_DOMAIN_SUFFIXES) or _path_has_marker(
        path, _PAPER_PATH_MARKERS
    ):
        return "paper"
    if (
        domain.startswith(("docs.", "developer.", "developers."))
        or _matches_exact_or_suffix(domain, _OFFICIAL_DOC_DOMAINS, _OFFICIAL_DOC_DOMAIN_SUFFIXES)
        or _path_has_marker(path, _OFFICIAL_DOC_PATH_MARKERS)
    ):
        return "official_docs"
    if (
        domain.startswith("blog.")
        or domain in _OFFICIAL_BLOG_DOMAINS
        or _path_has_marker(path, _OFFICIAL_BLOG_PATH_MARKERS)
    ):
        return "official_blog"
    if any(host in domain for host in ("news.ycombinator.com", "techcrunch.com", "theverge.com", "wired.com")):
        return "news"
    if any(host in domain for host in ("geeksforgeeks.org", "w3schools.com", "tutorialspoint.com")):
        return "aggregator"
    return "blog"


class WebSearchService:
    def __init__(
        self,
        search_provider: WebSearchProvider,
        fetch_provider: WebFetchProvider,
        config: Optional[WebSearchServiceConfig] = None,
    ) -> None:
        self._search_provider = search_provider
        self._fetch_provider = fetch_provider
        self._config = config or WebSearchServiceConfig()

    async def search(
        self,
        *,
        query: str,
        top_k: Optional[int] = None,
        freshness: str = "any",
        source_preferences: Optional[Iterable[str]] = None,
        language: str = "any",
    ) -> dict:
        normalized_query = " ".join((query or "").split()).strip()
        if not normalized_query:
            raise ValueError("Search query cannot be empty.")

        # 工具调用成功导向架构：query 退化兜底链。
        # 当原 query 触发空结果或上游异常时，自动收窄一次再试一次。
        # 退化仅做一次，避免无限退化；最终空结果仍按正常空返回。
        attempts: list[str] = [normalized_query]
        degraded = _degrade_query(normalized_query)
        if degraded and degraded != normalized_query:
            attempts.append(degraded)

        last_exception: Optional[Exception] = None
        last_result: Optional[dict] = None
        for attempt_query in attempts:
            try:
                payload = await self._do_search(
                    query=attempt_query,
                    top_k=top_k,
                    freshness=freshness,
                    source_preferences=source_preferences,
                    language=language,
                )
            except Exception as exc:
                last_exception = exc
                continue
            last_result = payload
            if payload.get("results"):
                return payload
        if last_result is not None:
            return last_result
        if last_exception is not None:
            raise last_exception
        # 既无异常又无结果 —— 返回空 payload，调用方按现有逻辑处理。
        return {
            "query": normalized_query,
            "results": [],
            "total_returned": 0,
            "truncated": False,
            "provider": self._config.provider_name,
            "search_time_ms": 0,
            "guidance": _search_guidance([], self._config),
        }

    async def _do_search(
        self,
        *,
        query: str,
        top_k: Optional[int] = None,
        freshness: str = "any",
        source_preferences: Optional[Iterable[str]] = None,
        language: str = "any",
    ) -> dict:
        normalized_query = " ".join((query or "").split()).strip()
        if not normalized_query:
            raise ValueError("Search query cannot be empty.")

        bounded_top_k = max(1, min(int(top_k or self._config.default_top_k), self._config.max_top_k))
        preferences = {item for item in (source_preferences or []) if item in self._config.allowed_source_types}
        started = time.monotonic()
        raw_results = await self._search_provider.search(
            normalized_query,
            top_k=max(bounded_top_k * 2, bounded_top_k + 3),
            language=language,
            freshness=freshness,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        ranked: dict[str, dict] = {}
        for index, raw in enumerate(raw_results):
            canonical_url = canonicalize_url(raw.url)
            if not canonical_url:
                continue
            source_type = classify_source(canonical_url)
            if self._config.allowed_source_types and source_type not in self._config.allowed_source_types:
                continue
            provider_score = raw.score or max(0.0, 1.0 - index * 0.05)
            trust_score = _TRUST_SCORES.get(source_type, 0.4)
            preference_bonus = 0.08 if source_type in preferences else 0.0
            score = round(provider_score * 0.45 + trust_score * 0.45 + preference_bonus, 4)
            candidate = {
                "id": f"r{len(ranked) + 1}",
                "title": " ".join((raw.title or "").split()) or canonical_url,
                "url": canonical_url,
                "snippet": _build_query_aware_excerpt(normalized_query, raw.snippet, raw.title),
                "domain": domain_from_url(canonical_url),
                "source_type": source_type,
                "published_at": raw.published_at,
                "score": score,
            }
            existing = ranked.get(canonical_url)
            if existing is None or candidate["score"] > existing["score"]:
                ranked[canonical_url] = candidate

        ordered = sorted(ranked.values(), key=lambda item: (-item["score"], item["domain"], item["title"]))
        truncated = len(ordered) > bounded_top_k
        results = ordered[:bounded_top_k]
        for index, item in enumerate(results, start=1):
            item["id"] = f"r{index}"

        return {
            "query": normalized_query,
            "results": results,
            "total_returned": len(results),
            "truncated": truncated,
            "provider": self._config.provider_name,
            "search_time_ms": elapsed_ms,
            "guidance": _search_guidance(results, self._config),
        }

    async def fetch(
        self,
        *,
        url: str,
        offset: int = 0,
        limit_chars: Optional[int] = None,
        section_cursor: str | None = None,
        query: str | None = None,
    ) -> dict:
        canonical_url = canonicalize_url(url)
        if not canonical_url:
            raise ValueError("Only http/https URLs are supported.")
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1000, min(int(limit_chars or self._config.default_limit_chars), 50000))

        page = await self._fetch_provider.fetch(canonical_url)
        if not page.content.strip():
            raise UnsupportedPageError("Fetched page has no readable content.")
        source_type = classify_source(canonical_url)

        normalized_query = (query or "").strip()
        if normalized_query and page.sections:
            query_aware_result = self._fetch_query_aware(
                canonical_url=canonical_url,
                page=page,
                source_type=source_type,
                query=normalized_query,
                bounded_limit=bounded_limit,
            )
            if query_aware_result is not None:
                return query_aware_result
            # 命中为 0 时降级到 section/offset 原路径,不让 ② 把"找不到相关段"
            # 变成空回复 —— 至少返回页面默认首段,让模型仍能拿到内容。

        if page.sections and (section_cursor is not None or bounded_offset == 0):
            section_index = _parse_section_cursor(section_cursor or "s0", len(page.sections))
            section = page.sections[section_index]
            section_offset = bounded_offset if section_cursor is not None else 0
            sliced = section.content[section_offset:section_offset + bounded_limit]
            within_section_truncated = section_offset + bounded_limit < len(section.content)
            next_section = page.sections[section_index + 1] if section_index + 1 < len(page.sections) else None
            next_section_cursor = section.cursor if within_section_truncated else (next_section.cursor if next_section else None)
            next_section_hint = (
                f"{section.heading} (continued)"
                if within_section_truncated
                else (next_section.heading if next_section else None)
            )
            next_offset = section_offset + bounded_limit if within_section_truncated else None
            return {
                "url": canonical_url,
                "title": page.title.strip() or domain_from_url(canonical_url),
                "domain": domain_from_url(canonical_url),
                "content": sliced,
                "content_type": page.content_type,
                "published_at": page.published_at,
                "source_type": source_type,
                "truncated": within_section_truncated or next_section is not None,
                "next_offset": next_offset,
                "offset": section_offset,
                "limit_chars": bounded_limit,
                "pagination_mode": "section",
                "section_cursor": section.cursor,
                "section_path": list(section.section_path),
                "headings": list(page.headings),
                "code_blocks": list(section.code_blocks),
                "next_section_cursor": next_section_cursor,
                "next_section_hint": next_section_hint,
                "available_sections": _section_summaries(page),
                "guidance": _fetch_guidance(
                    source_type=source_type,
                    pagination_mode="section",
                    truncated=within_section_truncated or next_section is not None,
                    code_blocks=list(section.code_blocks),
                    next_section_cursor=next_section_cursor,
                    section_path=list(section.section_path),
                    config=self._config,
                ),
            }

        content = page.content.strip()
        sliced = content[bounded_offset:bounded_offset + bounded_limit]
        truncated = bounded_offset + bounded_limit < len(content)
        next_offset = bounded_offset + bounded_limit if truncated else None

        return {
            "url": canonical_url,
            "title": page.title.strip() or domain_from_url(canonical_url),
            "domain": domain_from_url(canonical_url),
            "content": sliced,
            "content_type": page.content_type,
            "published_at": page.published_at,
            "source_type": source_type,
            "truncated": truncated,
            "next_offset": next_offset,
            "offset": bounded_offset,
            "limit_chars": bounded_limit,
            "pagination_mode": "offset",
            "section_cursor": None,
            "section_path": [],
            "headings": list(page.headings),
            "code_blocks": list(page.code_blocks),
            "next_section_cursor": None,
            "next_section_hint": None,
            "available_sections": _section_summaries(page),
            "guidance": _fetch_guidance(
                source_type=source_type,
                pagination_mode="offset",
                truncated=truncated,
                code_blocks=list(page.code_blocks),
                next_section_cursor=None,
                section_path=[],
                config=self._config,
            ),
        }

    def _fetch_query_aware(
        self,
        *,
        canonical_url: str,
        page,
        source_type: str,
        query: str,
        bounded_limit: int,
    ) -> dict | None:
        """按 query 在 page.sections 上做块级相关度打分,选 top-N 段拼接返回。

        没有任何段命中(score 全 0)时返回 None,让上层降级到 section/offset 原路径。
        命中段的累计字符不超过 bounded_limit,超过时按降序优先保留高分段。
        """
        scored: list[tuple[int, int, object]] = []
        for index, section in enumerate(page.sections):
            score = _score_section_against_query(query, section.content, section.heading)
            if score > 0:
                # -index 保证同分时保留页面原顺序
                scored.append((score, -index, section))
        if not scored:
            return None
        scored.sort(reverse=True)

        selected: list = []
        total_chars = 0
        for _score, _neg_index, section in scored:
            section_chars = len(section.content)
            if selected and total_chars + section_chars > bounded_limit:
                # 已经至少选了一段,再加这段会超额 —— 停下
                break
            selected.append(section)
            total_chars += section_chars
            if total_chars >= bounded_limit:
                break

        # 拼接选中段,带上 section_path 作为小标题,便于模型理解结构
        parts: list[str] = []
        for section in selected:
            heading_line = " > ".join(section.section_path) if section.section_path else section.heading
            parts.append(f"## {heading_line}\n\n{section.content}".rstrip())
        content = "\n\n".join(parts)
        if len(content) > bounded_limit:
            content = content[:bounded_limit]

        truncated = len(selected) < len(scored)
        selected_cursors = [section.cursor for section in selected]
        selected_paths = [list(section.section_path) for section in selected]
        selected_headings = list(dict.fromkeys(section.heading for section in selected))
        selected_code_blocks = list(
            dict.fromkeys(code for section in selected for code in section.code_blocks)
        )

        return {
            "url": canonical_url,
            "title": page.title.strip() or domain_from_url(canonical_url),
            "domain": domain_from_url(canonical_url),
            "content": content,
            "content_type": page.content_type,
            "published_at": page.published_at,
            "source_type": source_type,
            "truncated": truncated,
            "next_offset": None,
            "offset": 0,
            "limit_chars": bounded_limit,
            "pagination_mode": "query",
            "section_cursor": None,
            "section_path": [],
            "headings": selected_headings,
            "code_blocks": selected_code_blocks,
            "next_section_cursor": None,
            "next_section_hint": None,
            "available_sections": _section_summaries(page),
            "query": query,
            "selected_sections": selected_cursors,
            "selected_section_paths": selected_paths,
            "guidance": _fetch_guidance(
                source_type=source_type,
                pagination_mode="query",
                truncated=truncated,
                code_blocks=selected_code_blocks,
                next_section_cursor=None,
                section_path=selected_paths[0] if selected_paths else [],
                config=self._config,
            ),
        }


__all__ = [
    "DEFAULT_ALLOWED_SOURCE_TYPES",
    "WebSearchService",
    "WebSearchServiceConfig",
    "build_web_search_config",
    "canonicalize_url",
    "classify_source",
    "domain_from_url",
]
