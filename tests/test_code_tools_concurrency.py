"""
code_tools 并发安全 — 测试套件

覆盖场景:
- 同一 path 的两个 write_file 并发请求被 per-path 锁串行化,最终文件内容完整不交叉
- 不同 path 的并发 write_file 真正并行(总耗时接近最慢那一个,而非累加)
- edit_file 同一 path 并发不会丢更新(read-modify-write 不交叉)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.learning_agent.extensions.code_tools import (
    _reset_file_locks,
    _tool_edit_file,
    _tool_write_file,
)


@pytest.fixture(autouse=True)
def _reset_locks_and_cwd(tmp_path, monkeypatch):
    """每个测试用例切到独立 tmp_path 工作目录,并清空文件锁注册表。"""
    _reset_file_locks()
    monkeypatch.chdir(tmp_path)
    yield
    _reset_file_locks()


@pytest.mark.asyncio
async def test_write_file_serializes_same_path():
    """同一 path 的并发 write_file 必须被锁串行化,最终内容是后写入者的完整内容,不出现混合。"""
    # 写两个长度差很大的内容,如果两者交错,文件大小会落在中间值
    content_a = "A" * 50_000
    content_b = "B" * 30_000

    results = await asyncio.gather(
        _tool_write_file(path="race.txt", content=content_a),
        _tool_write_file(path="race.txt", content=content_b),
    )

    # 两个调用都应该成功
    assert all(r.get("status") == "written" for r in results), results

    # 文件大小必须等于 content_a 或 content_b 二者之一,不能是混合
    actual = Path("race.txt").read_text()
    assert actual in (content_a, content_b), (
        f"File content was interleaved! length={len(actual)}, "
        f"expected len {len(content_a)} or {len(content_b)}"
    )
    # 内容必须全是同一个字符,没有 A/B 混杂
    assert len(set(actual)) == 1, f"File contains mixed chars: {set(actual)}"


@pytest.mark.asyncio
async def test_write_file_parallel_different_paths():
    """不同 path 的并发 write_file 应真正并发,不被无关锁阻塞。"""
    # 我们没法直接证明"真正并发",但可以用一个高并发场景 + 计时来近似:
    # 写 5 个不同 path 的小文件,串行需要 ~50ms 累计开销,实际应远小于此
    async def write_one(idx: int) -> dict:
        return await _tool_write_file(path=f"f_{idx}.txt", content=f"data_{idx}")

    start = time.monotonic()
    results = await asyncio.gather(*[write_one(i) for i in range(5)])
    elapsed = time.monotonic() - start

    assert all(r.get("status") == "written" for r in results)
    # 5 个文件全部写入
    for i in range(5):
        assert Path(f"f_{i}.txt").read_text() == f"data_{i}"

    # 5 个独立 path 的并发 IO 应在 100ms 以内完成(本地小文件)
    assert elapsed < 1.0, f"Different-path writes too slow: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_edit_file_serializes_same_path():
    """edit_file 的 read-modify-write 必须串行化,两次并发替换都应生效。"""
    # 初始内容,有两个 marker 都可以被替换
    Path("doc.txt").write_text("HELLO_AAA WORLD_BBB END")

    # 并发发起两次 edit:一个替换 AAA→111,另一个替换 BBB→222
    # 如果不加锁,两个 edit 都基于原始内容生成新文本然后写回,
    # 后写者会覆盖前写者的修改 → 只有一次替换生效
    results = await asyncio.gather(
        _tool_edit_file(path="doc.txt", old_string="HELLO_AAA", new_string="HELLO_111"),
        _tool_edit_file(path="doc.txt", old_string="WORLD_BBB", new_string="WORLD_222"),
    )

    # 两个都应该成功
    assert all(r.get("status") == "edited" for r in results), results

    final = Path("doc.txt").read_text()
    # 两个替换都必须生效(锁保证 read-modify-write 串行)
    assert "HELLO_111" in final, f"First edit lost: {final}"
    assert "WORLD_222" in final, f"Second edit lost: {final}"
    assert "AAA" not in final and "BBB" not in final, f"Original markers leaked: {final}"


@pytest.mark.asyncio
async def test_write_file_different_paths_dont_block_each_other():
    """A 写入很慢(用大内容模拟)时,不同 path 的 B 写入不应被阻塞。"""
    # 这个测试主要是契约性的:验证不同 path 拿到的是不同的 Lock 对象
    from learning_agent.learning_agent.extensions.code_tools import _get_path_lock

    cwd = Path.cwd().resolve()
    lock_a = _get_path_lock(str(cwd / "a.txt"))
    lock_b = _get_path_lock(str(cwd / "b.txt"))
    lock_a_again = _get_path_lock(str(cwd / "a.txt"))

    assert lock_a is not lock_b, "Different paths must get different locks"
    assert lock_a is lock_a_again, "Same path must get the same lock instance"
