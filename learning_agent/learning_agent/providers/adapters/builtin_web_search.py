from __future__ import annotations

import asyncio
import ssl
from html.parser import HTMLParser
from typing import Optional
from urllib import error, parse, request

from learning_agent.learning_agent.providers.web_search_provider import (
    RawSearchResult,
    SearchTimeoutError,
    SearchUnavailableError,
)
from learning_agent.learning_agent.providers.adapters.tls_utils import (
    build_web_tls_context,
    tls_failure_hint,
)

_SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _normalize_result_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if raw_url.startswith("//"):
        raw_url = f"https:{raw_url}"
    parsed = parse.urlparse(raw_url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        uddg = parse.parse_qs(parsed.query).get("uddg")
        if uddg:
            return parse.unquote(uddg[0])
    return raw_url


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._current_kind: Optional[str] = None
        self._current_href: str = ""
        self._chunks: list[str] = []
        self._title_items: list[tuple[str, str]] = []
        self._snippet_items: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_map = dict(attrs)
        classes = attrs_map.get("class", "") or ""
        if "result__a" in classes:
            self._current_kind = "title"
            self._current_href = _normalize_result_url(attrs_map.get("href", "") or "")
            self._chunks = []
        elif "result__snippet" in classes:
            self._current_kind = "snippet"
            self._current_href = ""
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._current_kind:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._current_kind:
            return
        if tag not in {"a", "div", "span"}:
            return
        text = " ".join(" ".join(self._chunks).split())
        if not text:
            self._reset_capture()
            return
        if self._current_kind == "title" and self._current_href:
            self._title_items.append((text, self._current_href))
        elif self._current_kind == "snippet":
            self._snippet_items.append(text)
        self._reset_capture()

    def results(self, top_k: int) -> list[RawSearchResult]:
        results: list[RawSearchResult] = []
        for index, (title, url) in enumerate(self._title_items[:top_k]):
            snippet = self._snippet_items[index] if index < len(self._snippet_items) else ""
            results.append(
                RawSearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    score=max(0.0, 1.0 - index * 0.05),
                )
            )
        return results

    def _reset_capture(self) -> None:
        self._current_kind = None
        self._current_href = ""
        self._chunks = []


class BuiltinWebSearchProvider:
    """A lightweight HTML search provider using DuckDuckGo's HTML endpoint."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        user_agent: str = _DEFAULT_USER_AGENT,
        ca_bundle_path: str | None = None,
        prefer_system_trust_store: bool = True,
    ):
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent
        self._ssl_context = build_web_tls_context(
            ca_bundle_path=ca_bundle_path,
            prefer_system_trust_store=prefer_system_trust_store,
        )

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        language: str = "any",
        freshness: str = "any",
    ) -> list[RawSearchResult]:
        del freshness
        return await asyncio.to_thread(self._search_sync, query, top_k, language)

    def _search_sync(self, query: str, top_k: int, language: str) -> list[RawSearchResult]:
        params = {"q": query}
        language_region = {
            "zh": "cn-zh",
            "en": "us-en",
        }.get(language, "")
        if language_region:
            params["kl"] = language_region
        url = f"{_SEARCH_ENDPOINT}?{parse.urlencode(params)}"
        req = request.Request(
            url,
            headers={
                "User-Agent": self._user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with request.urlopen(req, timeout=self._timeout_seconds, context=self._ssl_context) as resp:
                payload = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
        except error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504}:
                raise SearchUnavailableError(f"Search upstream returned HTTP {exc.code}") from exc
            raise SearchUnavailableError(f"Search request failed with HTTP {exc.code}") from exc
        except error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                raise SearchTimeoutError("Search request timed out") from exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise SearchUnavailableError(
                    f"Search request failed: {reason}. {tls_failure_hint()}"
                ) from exc
            raise SearchUnavailableError(f"Search request failed: {reason or exc}") from exc
        except ssl.SSLCertVerificationError as exc:
            raise SearchUnavailableError(
                f"Search request failed: {exc}. {tls_failure_hint()}"
            ) from exc
        except TimeoutError as exc:
            raise SearchTimeoutError("Search request timed out") from exc

        parser_obj = _DuckDuckGoHTMLParser()
        parser_obj.feed(payload)
        return parser_obj.results(top_k)
