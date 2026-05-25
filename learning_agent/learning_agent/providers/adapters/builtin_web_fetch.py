from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from urllib import error, parse, request

from learning_agent.learning_agent.providers.web_fetch_provider import (
    FetchTimeoutError,
    FetchUnavailableError,
    RawFetchedPage,
    UnsupportedPageError,
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_READ_BYTES = 512 * 1024
_BLOCK_TAGS = {
    "article",
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "footer",
    "li",
    "main",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
    "ol",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg"}


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._title_chunks: list[str] = []
        self._body_chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self._body_chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self._body_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self._body_chunks.append(text)

    @property
    def title(self) -> str:
        return " ".join(" ".join(self._title_chunks).split())

    @property
    def content(self) -> str:
        lines = []
        for raw_line in "".join(self._body_chunks).splitlines():
            line = " ".join(raw_line.split())
            if line:
                lines.append(line)
        return "\n".join(lines)


class BuiltinWebFetchProvider:
    """A lightweight readable-content fetcher based on stdlib urllib + HTMLParser."""

    def __init__(self, *, timeout_seconds: float = 12.0, user_agent: str = _DEFAULT_USER_AGENT):
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent

    async def fetch(self, url: str) -> RawFetchedPage:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> RawFetchedPage:
        parsed = parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise UnsupportedPageError("Only http and https URLs are supported")

        req = request.Request(
            url,
            headers={
                "User-Agent": self._user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as resp:
                content_type = resp.headers.get_content_type()
                raw = resp.read(_MAX_READ_BYTES)
                charset = resp.headers.get_content_charset() or "utf-8"
        except error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise UnsupportedPageError("Page requires auth or is not accessible") from exc
            if exc.code in {429, 500, 502, 503, 504}:
                raise FetchUnavailableError(f"Fetch upstream returned HTTP {exc.code}") from exc
            raise FetchUnavailableError(f"Fetch request failed with HTTP {exc.code}") from exc
        except error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                raise FetchTimeoutError("Fetch request timed out") from exc
            raise FetchUnavailableError(f"Fetch request failed: {reason or exc}") from exc
        except TimeoutError as exc:
            raise FetchTimeoutError("Fetch request timed out") from exc

        text = raw.decode(charset, errors="replace")
        if content_type in {"text/plain", "application/json"}:
            content = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            title = parsed.netloc
            return RawFetchedPage(url=url, title=title, content=content, content_type=content_type)

        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise UnsupportedPageError(f"Unsupported content type: {content_type}")

        parser_obj = _ReadableHTMLParser()
        parser_obj.feed(text)
        content = parser_obj.content
        if not content:
            raise UnsupportedPageError("Failed to extract readable page content")
        title = parser_obj.title or parsed.netloc
        return RawFetchedPage(url=url, title=title, content=content, content_type=content_type)
