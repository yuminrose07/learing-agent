from __future__ import annotations

import asyncio

from tests.e2e.real_runner import RealRunner


def test_env_skip_preserves_planned_user_messages(monkeypatch):
    monkeypatch.delenv("LA_TIMEOUT", raising=False)
    runner = RealRunner()
    case = {
        "id": "manual-timeout",
        "title": "manual timeout",
        "setup": {"env_overrides": {"LA_TIMEOUT": "2"}},
        "turns": [{"role": "user", "message": "hello from turns"}],
    }
    try:
        result = asyncio.run(runner.run_case(case, "chat", True))
        assert result.verdict.verdict == "skipped"
        assert result.evidence.user_messages == ["hello from turns"]
        assert "LA_TIMEOUT" in (result.verdict.skipped_reason or "")
    finally:
        asyncio.run(runner.close())
