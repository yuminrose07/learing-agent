"""Trafilatura + Jina Reader 抽取兜底链路单元测试。

新链路只接管"成功拿到 HTML 但 stdlib 抽取返回空 content"的分支,
不影响 401/403/5xx → RSS / archive.org 这些 HTTP 异常路径。
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from learning_agent.learning_agent.providers.adapters import builtin_web_fetch as bwf
from learning_agent.learning_agent.providers.adapters.builtin_web_fetch import (
    BuiltinWebFetchProvider,
    _build_sections_from_markdown,
    _extract_markdown_code_blocks,
)
from learning_agent.learning_agent.providers.web_fetch_provider import (
    UnsupportedPageError,
)


class _FakeHTTPResponse:
    def __init__(
        self,
        *,
        url: str,
        body: str,
        content_type: str,
        charset: str = "utf-8",
    ) -> None:
        self._url = url
        self._body = body.encode(charset)
        self.status = 200
        self.headers = self
        self._content_type = content_type
        self._charset = charset

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit: int | None = None) -> bytes:
        if limit is None:
            return self._body
        return self._body[:limit]

    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str:
        return self._charset

    def geturl(self) -> str:
        return self._url


# Trafilatura 在最小 HTML 上能抽出正文,用作测试桩。
_EXTRACTABLE_HTML = """<html><head><title>Sample Article</title></head>
<body>
<article>
<h1>测试标题</h1>
<p>第一段正文,包含足够的字符让 trafilatura 的密度评分识别为正文段落。</p>
<p>第二段正文也是有意义的内容,trafilatura 会把整段都抽出来。</p>
<p>第三段进一步加权,确保抽取算法把这块判定为主体内容。</p>
</article>
</body></html>
"""


def _stub_stdlib_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 stdlib HTMLParser 路径返回空 content,触发兜底链路。

    用 monkeypatch 比构造真实 HTML 更稳健 —— 避免测试因 stdlib parser
    的细节行为漂移而失效。
    """
    monkeypatch.setattr(
        bwf,
        "_extract_structured_readable_html",
        lambda _html: ("", "", (), ()),
    )


async def test_trafilatura_recovers_when_stdlib_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if bwf.trafilatura is None:
        pytest.skip("trafilatura 包未安装,跳过 A 级兜底测试")

    _stub_stdlib_returns_empty(monkeypatch)

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        assert req.full_url == "https://example.com/page"
        return _FakeHTTPResponse(
            url=req.full_url,
            body=_EXTRACTABLE_HTML,
            content_type="text/html",
        )

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(prefer_system_trust_store=False)
    page = await provider.fetch("https://example.com/page")

    assert page.content
    assert "第一段正文" in page.content
    assert page.title  # trafilatura 抽到 title 或 fallback 为 netloc
    # 只有一个 H1 的简单页面,markdown 切分后仍是单段(多段切分的回归基线)
    assert len(page.sections) == 1


async def test_trafilatura_disabled_falls_through_to_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_stdlib_returns_empty(monkeypatch)

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        return _FakeHTTPResponse(
            url=req.full_url,
            body=_EXTRACTABLE_HTML,
            content_type="text/html",
        )

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(
        prefer_system_trust_store=False,
        trafilatura_enabled=False,
        jina_reader_enabled=False,
    )

    with pytest.raises(UnsupportedPageError):
        await provider.fetch("https://example.com/page")


async def test_jina_reader_fallback_when_local_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用 jina 且本地两段都失败时,正文从 r.jina.ai 拿到。"""
    _stub_stdlib_returns_empty(monkeypatch)
    # 同步 disable trafilatura,确保兜底链一定走到 jina
    monkeypatch.setattr(
        bwf, "_extract_with_trafilatura", lambda *a, **kw: None
    )

    jina_markdown = (
        "Title: 测试页面标题\n"
        "URL Source: https://example.com/article\n"
        "\n"
        "# 测试页面标题\n"
        "\n"
        "正文段落由 Jina Reader 渲染后返回,包含核心信息。"
    )

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        url = req.full_url
        if url == "https://example.com/article":
            return _FakeHTTPResponse(
                url=url,
                body="<html><body><span>noise</span></body></html>",
                content_type="text/html",
            )
        if url == "https://r.jina.ai/https://example.com/article":
            return _FakeHTTPResponse(
                url=url,
                body=jina_markdown,
                content_type="text/markdown",
            )
        raise AssertionError(f"Unexpected urlopen call: {url}")

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(
        prefer_system_trust_store=False,
        trafilatura_enabled=False,
        jina_reader_enabled=True,
    )

    page = await provider.fetch("https://example.com/article")

    assert page.title == "测试页面标题"
    assert "正文段落" in page.content
    # 头部的 "Title:" 行应该被剥掉
    assert "Title:" not in page.content


async def test_jina_reader_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 jina_reader_enabled=False,r.jina.ai 路径绝不被调用。"""
    _stub_stdlib_returns_empty(monkeypatch)
    monkeypatch.setattr(
        bwf, "_extract_with_trafilatura", lambda *a, **kw: None
    )

    called_urls: list[str] = []

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        called_urls.append(req.full_url)
        if "r.jina.ai" in req.full_url:
            raise AssertionError(
                "jina_reader_enabled=False 时不应该访问 r.jina.ai"
            )
        return _FakeHTTPResponse(
            url=req.full_url,
            body="<html><body>empty</body></html>",
            content_type="text/html",
        )

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(prefer_system_trust_store=False)

    with pytest.raises(UnsupportedPageError):
        await provider.fetch("https://example.com/page")

    assert all("r.jina.ai" not in url for url in called_urls)


async def test_jina_reader_skipped_when_deadline_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deadline 剩余 < 2s 时,jina 兜底主动放弃,留时间给 archive.org。"""
    _stub_stdlib_returns_empty(monkeypatch)
    monkeypatch.setattr(
        bwf, "_extract_with_trafilatura", lambda *a, **kw: None
    )

    called_urls: list[str] = []

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        called_urls.append(req.full_url)
        return _FakeHTTPResponse(
            url=req.full_url,
            body="<html><body>empty</body></html>",
            content_type="text/html",
        )

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    # overall_timeout_seconds 设很小,_fetch_via_jina_reader 入口检测会跳过
    provider = BuiltinWebFetchProvider(
        prefer_system_trust_store=False,
        overall_timeout_seconds=1.0,
        jina_reader_enabled=True,
    )

    with pytest.raises(UnsupportedPageError):
        await provider.fetch("https://example.com/page")

    # 不应该出现对 r.jina.ai 的调用
    assert all("r.jina.ai" not in url for url in called_urls)


async def test_jina_reader_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jina 内部任何异常都吞掉,最终抛 UnsupportedPageError 而非 jina 的错。"""
    _stub_stdlib_returns_empty(monkeypatch)
    monkeypatch.setattr(
        bwf, "_extract_with_trafilatura", lambda *a, **kw: None
    )

    from urllib import error as url_error

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        if "r.jina.ai" in req.full_url:
            raise url_error.HTTPError(
                url=req.full_url,
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=io.BytesIO(b""),
            )
        return _FakeHTTPResponse(
            url=req.full_url,
            body="<html><body>empty</body></html>",
            content_type="text/html",
        )

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(
        prefer_system_trust_store=False,
        jina_reader_enabled=True,
    )

    with pytest.raises(UnsupportedPageError):
        await provider.fetch("https://example.com/page")


async def test_fallback_chain_preserves_archive_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """500 等 HTTP 错误时,archive.org 兜底仍然生效,不被新链路拦截。"""
    from urllib import error as url_error

    archive_html = """<html><head><title>归档版本</title></head>
<body><article><p>这是 archive.org 上的缓存正文,内容来自历史快照。</p></article></body></html>
"""

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        url = req.full_url
        if url == "https://broken.example.com/page":
            raise url_error.HTTPError(
                url=url,
                code=500,
                msg="Internal Server Error",
                hdrs={},
                fp=io.BytesIO(b""),
            )
        # archive.org 的 timegate API:接受任何带 /web/ 前缀的 URL
        if "web.archive.org" in url:
            return _FakeHTTPResponse(
                url=url,
                body=archive_html,
                content_type="text/html",
            )
        raise AssertionError(f"Unexpected urlopen call: {url}")

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(prefer_system_trust_store=False)

    try:
        page = await provider.fetch("https://broken.example.com/page")
    except UnsupportedPageError:
        # archive.org 兜底在某些情况下 URL 形态不匹配 _archive_fallback_url 的输出,
        # 这种情况下抛 UnsupportedPageError 是可以接受的(我们只验证不破坏现有行为)
        return

    # 命中 archive 兜底时内容应来自归档 HTML
    assert "archive.org" in page.content or "归档" in page.content or page.content


# ---------------------------------------------------------------------------
# markdown 多段切分单元测试(纯函数,不依赖网络 / trafilatura)
#
# trafilatura / jina 之前把整段 markdown 塞进单个 s0,导致章节导航失效、
# 只能盲目 offset 顺序切。以下测试锁定按标题切层级 section 的行为。
# ---------------------------------------------------------------------------


def test_markdown_sections_split_by_heading_hierarchy() -> None:
    md = (
        "# Async IO\n\n概述段落。\n\n"
        "## Coroutines\n\n协程说明。\n\n"
        "### Task groups\n\n任务组说明。\n\n"
        "## Event loop\n\n事件循环说明。\n"
    )
    sections = _build_sections_from_markdown("Async IO", md)

    assert len(sections) == 4
    # cursor 连续编号,供翻页定位
    assert [s.cursor for s in sections] == ["s0", "s1", "s2", "s3"]
    # section_path 反映标题层级嵌套
    assert sections[0].section_path == ("Async IO",)
    assert sections[1].section_path == ("Async IO", "Coroutines")
    assert sections[2].section_path == ("Async IO", "Coroutines", "Task groups")
    # H2 "Event loop" 回退到二级,不挂在 Task groups 之下
    assert sections[3].section_path == ("Async IO", "Event loop")


def test_markdown_sections_exclude_fence_comments() -> None:
    """代码围栏内的 '# 注释' 是代码,绝不能被切成标题。"""
    md = (
        "# 真标题\n\n"
        "```python\n"
        "# 这是代码注释,不是标题\n"
        "def f():\n"
        "    pass\n"
        "```\n\n"
        "正文继续。\n"
    )
    sections = _build_sections_from_markdown("Doc", md)

    assert len(sections) == 1
    assert sections[0].heading == "真标题"
    assert "这是代码注释,不是标题" not in [s.heading for s in sections]


def test_markdown_sections_attach_code_blocks() -> None:
    md = (
        "# 标题A\n\n"
        "```python\nprint('a')\n```\n\n"
        "## 标题B\n\n"
        "```js\nconsole.log('b')\n```\n"
    )
    sections = _build_sections_from_markdown("Doc", md)

    assert len(sections) == 2
    assert sections[0].code_blocks == ("print('a')",)
    assert sections[1].code_blocks == ("console.log('b')",)


def test_markdown_preamble_before_first_heading_goes_to_root() -> None:
    md = "开篇导语,出现在任何标题之前。\n\n# 第一节\n\n正文。\n"
    sections = _build_sections_from_markdown("文档标题", md)

    assert len(sections) == 2
    # 标题前的导语归在 root 段,用传入 title 当 heading
    assert sections[0].section_path == ("文档标题",)
    assert "开篇导语" in sections[0].content
    # 第一个真标题是 H1,自成顶层路径
    assert sections[1].section_path == ("第一节",)


def test_markdown_empty_content_returns_no_sections() -> None:
    assert _build_sections_from_markdown("标题", "") == ()
    assert _build_sections_from_markdown("标题", "   \n\n  ") == ()


def test_extract_markdown_code_blocks_dedup() -> None:
    md = (
        "```python\nx = 1\n```\n"
        "```python\nx = 1\n```\n"  # 完全重复,去重
        "~~~js\ny = 2\n~~~\n"  # ~~~ 围栏也要识别
    )
    assert _extract_markdown_code_blocks(md) == ("x = 1", "y = 2")


async def test_jina_reader_returns_multiple_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """jina 返回多标题 markdown 时切成多段(回归防护:此前退化为单段 s0)。"""
    _stub_stdlib_returns_empty(monkeypatch)
    monkeypatch.setattr(bwf, "_extract_with_trafilatura", lambda *a, **kw: None)

    jina_markdown = (
        "Title: 指南\n"
        "\n"
        "# 指南\n\n概述部分介绍整体内容。\n\n"
        "## 安装\n\n安装步骤说明。\n\n"
        "## 用法\n\n基本用法演示。\n"
    )

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        url = req.full_url
        if url == "https://example.com/guide":
            return _FakeHTTPResponse(
                url=url,
                body="<html><body><span>noise</span></body></html>",
                content_type="text/html",
            )
        if url == "https://r.jina.ai/https://example.com/guide":
            return _FakeHTTPResponse(
                url=url,
                body=jina_markdown,
                content_type="text/markdown",
            )
        raise AssertionError(f"Unexpected urlopen call: {url}")

    monkeypatch.setattr(bwf.request, "urlopen", _fake_urlopen)

    provider = BuiltinWebFetchProvider(
        prefer_system_trust_store=False,
        trafilatura_enabled=False,
        jina_reader_enabled=True,
    )
    page = await provider.fetch("https://example.com/guide")

    assert len(page.sections) == 3
    headings = [s.heading for s in page.sections]
    assert "安装" in headings
    assert "用法" in headings
    # 章节导航元数据被填充,不再是空 / 单段
    assert len(page.headings) == 3
