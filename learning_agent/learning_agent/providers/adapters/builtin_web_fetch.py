from __future__ import annotations

import asyncio
import html
import re
import ssl
import time
from html.parser import HTMLParser
from urllib import error, parse, request
from xml.etree import ElementTree

from learning_agent.learning_agent.providers.web_fetch_provider import (
    FetchTimeoutError,
    FetchUnavailableError,
    RawFetchedPage,
    RawFetchedSection,
    UnsupportedPageError,
)
from learning_agent.learning_agent.providers.adapters.tls_utils import (
    build_web_tls_context,
    tls_failure_hint,
)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_READ_BYTES = 512 * 1024
_MAX_XML_READ_BYTES = 2 * 1024 * 1024
_BLOCK_TAGS = {
    "article",
    "blockquote",
    "br",
    "code",
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
    "td",
    "th",
    "tr",
    "ul",
    "ol",
}
_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "iframe"}
_NOISE_TAGS = {"aside", "button", "dialog", "footer", "form", "header", "nav"}
_NOISE_ATTR_MARKERS = (
    "advert",
    "banner",
    "breadcrumb",
    "consent",
    "cookie",
    "footer",
    "header",
    "hero",
    "language",
    "locale",
    "menu",
    "nav",
    "pagination",
    "promo",
    "recommend",
    "related",
    "search",
    "share",
    "sidebar",
    "social",
    "sponsor",
    "subscribe",
    "table-of-contents",
    "toc",
    "toolbar",
)
_POSITIVE_ATTR_MARKERS = (
    "api",
    "article",
    "body",
    "content",
    "doc",
    "docs",
    "documentation",
    "guide",
    "main",
    "manual",
    "markdown",
    "post",
    "reference",
    "tutorial",
)
_MIN_PRIORITY_CONTENT_CHARS = 200
_HEADING_TAG_LEVELS = {f"h{level}": level for level in range(1, 7)}
_XML_CONTENT_TYPES = {"text/xml", "application/xml", "application/rss+xml", "application/atom+xml"}
_FEED_LISTING_SEGMENTS = {"blog", "news", "stories", "updates"}
_FEED_SEGMENT_ALIASES = {
    "blog": ("news",),
}
_MAX_FEED_ITEMS = 8


def _normalize_attr_text(attrs: list[tuple[str, str | None]]) -> str:
    values: list[str] = []
    for key, value in attrs:
        if not value:
            continue
        if key in {"class", "id", "role", "aria-label", "data-testid"}:
            values.append(value.lower())
    return " ".join(values)


def _is_noise_container(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in _NOISE_TAGS:
        return True
    attr_text = _normalize_attr_text(attrs)
    return any(marker in attr_text for marker in _NOISE_ATTR_MARKERS)


def _is_priority_container(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in {"article", "main"}:
        return True
    attr_text = _normalize_attr_text(attrs)
    return any(marker in attr_text for marker in _POSITIVE_ATTR_MARKERS)


def _normalize_preformatted_text(data: str) -> str:
    stripped = data.strip("\n")
    if not stripped.strip():
        return ""
    return "\n".join(line.rstrip() for line in stripped.splitlines())


def _normalize_chunks(chunks: list[str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in "".join(chunks).splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def _normalize_feed_text(value: str | None) -> str:
    if not value:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    unescaped = html.unescape(without_tags)
    return " ".join(unescaped.split())


def _xml_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _xml_find_child_text(element: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if _xml_local_name(child.tag).lower() in wanted:
            return _normalize_feed_text("".join(child.itertext()))
    return ""


def _build_feed_sections(
    *,
    title: str,
    items: list[dict[str, str]],
) -> tuple[str, tuple[RawFetchedSection, ...], tuple[str, ...], str | None]:
    sections: list[RawFetchedSection] = []
    headings: list[str] = []
    lines: list[str] = []
    published_at = items[0].get("published_at") if items else None
    for index, item in enumerate(items[:_MAX_FEED_ITEMS]):
        heading = item.get("title") or f"Entry {index + 1}"
        headings.append(heading)
        block_lines = [heading]
        if item.get("published_at"):
            block_lines.append(f"Published: {item['published_at']}")
        if item.get("link"):
            block_lines.append(f"Link: {item['link']}")
        if item.get("summary"):
            block_lines.append(item["summary"])
        section_content = "\n".join(line for line in block_lines if line).strip()
        if not section_content:
            continue
        sections.append(
            RawFetchedSection(
                cursor=f"s{len(sections)}",
                heading=heading,
                section_path=(title, heading),
                content=section_content,
            )
        )
        lines.append(section_content)
    return "\n\n".join(lines).strip(), tuple(sections), tuple(headings), published_at


def _parse_feed_payload(url: str, text: str, content_type: str) -> RawFetchedPage:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise UnsupportedPageError("Failed to parse XML feed content") from exc

    root_name = _xml_local_name(root.tag).lower()
    channel_title = ""
    items: list[dict[str, str]] = []

    if root_name == "rss":
        channel = next((child for child in list(root) if _xml_local_name(child.tag).lower() == "channel"), None)
        if channel is None:
            raise UnsupportedPageError("RSS feed is missing a channel element")
        channel_title = _xml_find_child_text(channel, "title") or parse.urlparse(url).netloc
        for item in list(channel):
            if _xml_local_name(item.tag).lower() != "item":
                continue
            items.append(
                {
                    "title": _xml_find_child_text(item, "title"),
                    "link": _xml_find_child_text(item, "link"),
                    "published_at": _xml_find_child_text(item, "pubDate", "published", "updated"),
                    "summary": _xml_find_child_text(item, "description", "content", "summary"),
                }
            )
    elif root_name == "feed":
        channel_title = _xml_find_child_text(root, "title") or parse.urlparse(url).netloc
        for entry in list(root):
            if _xml_local_name(entry.tag).lower() != "entry":
                continue
            link = ""
            for child in list(entry):
                if _xml_local_name(child.tag).lower() != "link":
                    continue
                href = child.attrib.get("href")
                if href:
                    link = href.strip()
                    break
            items.append(
                {
                    "title": _xml_find_child_text(entry, "title"),
                    "link": link,
                    "published_at": _xml_find_child_text(entry, "published", "updated"),
                    "summary": _xml_find_child_text(entry, "summary", "content"),
                }
            )
    else:
        raise UnsupportedPageError(f"Unsupported XML feed root: {root_name}")

    content, sections, headings, published_at = _build_feed_sections(title=channel_title, items=items)
    if not content:
        raise UnsupportedPageError("XML feed has no readable entries")
    return RawFetchedPage(
        url=url,
        title=channel_title,
        content=content,
        content_type=content_type,
        published_at=published_at,
        headings=headings,
        sections=sections,
    )


def _feed_fallback_candidates(url: str) -> list[str]:
    parsed = parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    clean_path = parsed.path.rstrip("/")
    if clean_path.endswith((".xml", "/rss", "/feed", "/rss.xml", "/feed.xml")):
        return []
    segments = [segment for segment in clean_path.split("/") if segment]
    candidates: list[str] = []

    def add_candidate(path: str) -> None:
        candidate = parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        if candidate not in candidates:
            candidates.append(candidate)

    if clean_path:
        add_candidate(f"{clean_path}/rss.xml")
        add_candidate(f"{clean_path}/feed.xml")
    if segments and segments[-1].lower() in _FEED_LISTING_SEGMENTS:
        add_candidate(f"/{segments[-1]}/rss.xml")
        add_candidate(f"/{segments[-1]}/feed.xml")
        for alias in _FEED_SEGMENT_ALIASES.get(segments[-1].lower(), ()):
            add_candidate(f"/{alias}/rss.xml")
            add_candidate(f"/{alias}/feed.xml")
    add_candidate("/rss.xml")
    return candidates


def _archive_fallback_url(url: str) -> str | None:
    """生成 archive.org Wayback Machine 的最新快照 URL，用作不可达源的最后兜底。

    `web.archive.org` 自带可读的 HTML 视图，命中率高且基本不挂。
    """
    parsed = parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.netloc.endswith("archive.org") or parsed.netloc.endswith("web.archive.org"):
        return None
    return f"https://web.archive.org/web/2/{url}"


class _ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._title_chunks: list[str] = []
        self._fallback_chunks: list[str] = []
        self._priority_chunks: list[str] = []
        self._heading_entries: list[tuple[int, str]] = []
        self._code_blocks: list[str] = []
        self._skip_depth = 0
        self._priority_depth = 0
        self._preformatted_depth = 0
        self._in_title = False
        self._current_heading_level: int | None = None
        self._current_heading_chunks: list[str] = []
        self._current_code_chunks: list[str] = []
        self._tag_stack: list[tuple[str, bool, bool, bool, int | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        inherited_skip = self._skip_depth > 0
        should_skip = inherited_skip or tag in _SKIP_TAGS or _is_noise_container(tag, attrs)
        is_priority = False
        is_preformatted = tag in {"pre", "code"}
        heading_level = _HEADING_TAG_LEVELS.get(tag) if not should_skip else None
        if should_skip:
            self._skip_depth += 1
        else:
            is_priority = _is_priority_container(tag, attrs)
            if is_priority:
                self._priority_depth += 1
            if is_preformatted:
                if self._preformatted_depth == 0:
                    self._current_code_chunks = []
                self._preformatted_depth += 1
            if heading_level is not None:
                self._current_heading_level = heading_level
                self._current_heading_chunks = []
            if tag in _BLOCK_TAGS:
                self._append_break()
        self._tag_stack.append((tag, should_skip, is_priority, is_preformatted, heading_level))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if not self._tag_stack:
            return
        start_tag, was_skipped, was_priority, was_preformatted, heading_level = self._tag_stack.pop()
        del start_tag
        if was_skipped:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if heading_level is not None and self._current_heading_level == heading_level:
            heading = " ".join(" ".join(self._current_heading_chunks).split())
            if heading:
                self._heading_entries.append((heading_level, heading))
            self._current_heading_level = None
            self._current_heading_chunks = []
        if tag in _BLOCK_TAGS:
            self._append_break()
        if was_preformatted:
            self._preformatted_depth = max(0, self._preformatted_depth - 1)
            if self._preformatted_depth == 0:
                code_block = _normalize_preformatted_text("".join(self._current_code_chunks))
                if code_block and code_block not in self._code_blocks:
                    self._code_blocks.append(code_block)
                self._current_code_chunks = []
        if was_priority:
            self._priority_depth = max(0, self._priority_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)
            return
        if self._skip_depth:
            return
        if self._preformatted_depth:
            text = _normalize_preformatted_text(data)
            self._current_code_chunks.append(data)
        else:
            text = " ".join(data.split())
        if text:
            self._fallback_chunks.append(text)
            if self._priority_depth:
                self._priority_chunks.append(text)
            if self._current_heading_level is not None:
                self._current_heading_chunks.append(text)

    def _append_break(self) -> None:
        self._fallback_chunks.append("\n")
        if self._priority_depth:
            self._priority_chunks.append("\n")

    @property
    def title(self) -> str:
        return " ".join(" ".join(self._title_chunks).split())

    @property
    def content(self) -> str:
        fallback_content = _normalize_chunks(self._fallback_chunks)
        priority_content = _normalize_chunks(self._priority_chunks)
        if len(priority_content) >= max(_MIN_PRIORITY_CONTENT_CHARS, len(fallback_content) // 3):
            return priority_content
        return fallback_content

    @property
    def headings(self) -> list[tuple[int, str]]:
        return list(self._heading_entries)

    @property
    def code_blocks(self) -> list[str]:
        return list(self._code_blocks)


def _section_code_blocks(content: str, code_blocks: list[str]) -> tuple[str, ...]:
    matched: list[str] = []
    for code_block in code_blocks:
        if code_block and code_block in content and code_block not in matched:
            matched.append(code_block)
    return tuple(matched)


def _build_sections(title: str, content: str, headings: list[tuple[int, str]], code_blocks: list[str]) -> tuple[RawFetchedSection, ...]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return ()

    heading_index = 0
    heading_stack: list[str] = []
    sections: list[RawFetchedSection] = []
    current_heading = title.strip() or "Overview"
    current_path: tuple[str, ...] = (current_heading,) if current_heading else ()
    current_lines: list[str] = []

    def flush_section() -> None:
        nonlocal current_lines
        section_content = "\n".join(current_lines).strip()
        if not section_content:
            current_lines = []
            return
        cursor = f"s{len(sections)}"
        sections.append(
            RawFetchedSection(
                cursor=cursor,
                heading=current_heading,
                section_path=current_path,
                content=section_content,
                code_blocks=_section_code_blocks(section_content, code_blocks),
            )
        )
        current_lines = []

    for line in lines:
        if heading_index < len(headings) and line == headings[heading_index][1]:
            flush_section()
            level, heading_text = headings[heading_index]
            while len(heading_stack) >= level:
                heading_stack.pop()
            heading_stack.append(heading_text)
            current_heading = heading_text
            current_path = tuple(heading_stack)
            current_lines = [heading_text]
            heading_index += 1
            continue
        current_lines.append(line)

    flush_section()
    return tuple(sections)


def _extract_readable_html(html: str) -> tuple[str, str]:
    title, content, _, _ = _extract_structured_readable_html(html)
    return title, content


def _extract_structured_readable_html(html: str) -> tuple[str, str, tuple[RawFetchedSection, ...], tuple[str, ...]]:
    parser_obj = _ReadableHTMLParser()
    parser_obj.feed(html)
    parser_obj.close()
    title = parser_obj.title
    content = re.sub(r"\n{3,}", "\n\n", parser_obj.content).strip()
    sections = _build_sections(title, content, parser_obj.headings, parser_obj.code_blocks)
    headings = tuple(heading for _, heading in parser_obj.headings)
    return title, content, sections, headings


class BuiltinWebFetchProvider:
    """A lightweight readable-content fetcher based on stdlib urllib + HTMLParser."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        overall_timeout_seconds: float = 30.0,
        user_agent: str = _DEFAULT_USER_AGENT,
        ca_bundle_path: str | None = None,
        prefer_system_trust_store: bool = True,
    ):
        self._timeout_seconds = timeout_seconds
        # 整体墙钟预算:原站请求 + 多个 feed 候选 + archive 兜底共享同一份时间预算,
        # 防止级联回退把单次 fetch 拖到 60s+。
        self._overall_timeout_seconds = overall_timeout_seconds
        self._user_agent = user_agent
        self._ssl_context = build_web_tls_context(
            ca_bundle_path=ca_bundle_path,
            prefer_system_trust_store=prefer_system_trust_store,
        )

    async def fetch(self, url: str) -> RawFetchedPage:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _try_archive_fallback(
        self,
        url: str,
        allow_feed_fallback: bool,
        deadline: float,
    ) -> RawFetchedPage | None:
        """Wayback Machine 兜底:原站不可达时尝试公开快照。

        仅在首次进入 ``_fetch_sync`` 时启用(``allow_feed_fallback=True``),
        防止递归。命中后用 archive.org 的快照内容回放给上层。
        """
        if not allow_feed_fallback:
            return None
        if deadline - time.monotonic() <= 0:
            return None
        archived_url = _archive_fallback_url(url)
        if archived_url is None:
            return None
        try:
            return self._fetch_sync(
                archived_url,
                allow_feed_fallback=False,
                deadline=deadline,
            )
        except (UnsupportedPageError, FetchUnavailableError, FetchTimeoutError):
            return None
        except Exception:
            # archive fallback 是兜底，自身失败永远不抛 —— 让上层走原始失败语义。
            return None

    def _read_url_sync(
        self,
        url: str,
        *,
        timeout: float | None = None,
    ) -> tuple[str, bytes, str, str]:
        req = request.Request(
            url,
            headers={
                "User-Agent": self._user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        effective_timeout = timeout if timeout is not None else self._timeout_seconds
        with request.urlopen(req, timeout=effective_timeout, context=self._ssl_context) as resp:
            content_type = resp.headers.get_content_type()
            max_read_bytes = _MAX_XML_READ_BYTES if content_type in _XML_CONTENT_TYPES else _MAX_READ_BYTES
            raw = resp.read(max_read_bytes)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.geturl(), content_type, raw, charset

    def _fetch_sync(
        self,
        url: str,
        *,
        allow_feed_fallback: bool = True,
        deadline: float | None = None,
    ) -> RawFetchedPage:
        # 在最外层调用处建立墙钟 deadline,然后向所有递归路径(feed 候选、archive 兜底)
        # 透传同一份 deadline,使得所有 fallback 共享一份时间预算。
        if deadline is None:
            deadline = time.monotonic() + self._overall_timeout_seconds

        parsed = parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UnsupportedPageError("Only http and https URLs are supported")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchTimeoutError("Fetch wall-clock deadline exceeded")
        per_request_timeout = min(self._timeout_seconds, remaining)

        try:
            final_url, content_type, raw, charset = self._read_url_sync(
                url, timeout=per_request_timeout
            )
        except error.HTTPError as exc:
            if exc.code in {401, 403}:
                if allow_feed_fallback:
                    for candidate in _feed_fallback_candidates(url):
                        if deadline - time.monotonic() <= 0:
                            break
                        try:
                            return self._fetch_sync(
                                candidate,
                                allow_feed_fallback=False,
                                deadline=deadline,
                            )
                        except (UnsupportedPageError, FetchUnavailableError, FetchTimeoutError):
                            continue
                # 401/403 时 archive.org 也可能有公开快照
                archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
                if archived is not None:
                    return archived
                raise UnsupportedPageError("Page requires auth or is not accessible") from exc
            if exc.code in {429, 500, 502, 503, 504}:
                archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
                if archived is not None:
                    return archived
                raise FetchUnavailableError(f"Fetch upstream returned HTTP {exc.code}") from exc
            archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
            if archived is not None:
                return archived
            raise FetchUnavailableError(f"Fetch request failed with HTTP {exc.code}") from exc
        except error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
                if archived is not None:
                    return archived
                raise FetchTimeoutError("Fetch request timed out") from exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
                if archived is not None:
                    return archived
                raise FetchUnavailableError(
                    f"Fetch request failed: {reason}. {tls_failure_hint()}"
                ) from exc
            archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
            if archived is not None:
                return archived
            raise FetchUnavailableError(f"Fetch request failed: {reason or exc}") from exc
        except ssl.SSLCertVerificationError as exc:
            archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
            if archived is not None:
                return archived
            raise FetchUnavailableError(
                f"Fetch request failed: {exc}. {tls_failure_hint()}"
            ) from exc
        except TimeoutError as exc:
            archived = self._try_archive_fallback(url, allow_feed_fallback, deadline)
            if archived is not None:
                return archived
            raise FetchTimeoutError("Fetch request timed out") from exc

        text = raw.decode(charset, errors="replace")
        canonical_url = final_url or url
        if content_type in {"text/plain", "application/json"}:
            content = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            title = parse.urlparse(canonical_url).netloc
            return RawFetchedPage(
                url=canonical_url,
                title=title,
                content=content,
                content_type=content_type,
                sections=(
                    RawFetchedSection(
                        cursor="s0",
                        heading=title,
                        section_path=(title,),
                        content=content,
                    ),
                )
                if content
                else (),
            )

        if content_type in _XML_CONTENT_TYPES:
            return _parse_feed_payload(canonical_url, text, content_type)

        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise UnsupportedPageError(f"Unsupported content type: {content_type}")

        title, content, sections, headings = _extract_structured_readable_html(text)
        if not content:
            raise UnsupportedPageError("Failed to extract readable page content")
        title = title or parse.urlparse(canonical_url).netloc
        code_blocks = tuple({code for section in sections for code in section.code_blocks})
        return RawFetchedPage(
            url=canonical_url,
            title=title,
            content=content,
            content_type=content_type,
            headings=headings,
            sections=sections,
            code_blocks=code_blocks,
        )
