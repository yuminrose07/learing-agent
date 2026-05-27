"""TeachQuestionGenerator 单元测试（B5 E2）。

覆盖：
- LLM 返回标准 JSON 数组 → 构造 list[TeachQuestion]，kind="sa"
- concept_id 不在 concept_list → 丢弃
- 同一 concept_id 重复 → 只保留第一条
- 缺字段（stem/rubric/concept_id 空） → 跳过
- max_questions 截断
- 模型未解析 → 空列表 + 不抛
- 空 concepts → 直接短路，不调 provider
- provider 抛错 → 返回 []，不向上传播
- markdown 围栏容忍
- model_name 透传 / 回退 default_model / 都没有时跳过
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.learning_unit import ConceptItem, TeachQuestion
from learning_agent.ai.models import ChatChunk, ChatParams
from learning_agent.learning_agent.teach_generator import (
    MAX_QUESTIONS_PER_SESSION,
    TeachQuestionGenerator,
)


def _make_provider(
    raw_response: str,
    *,
    default_model: str | None = "kimi-2.6",
) -> MagicMock:
    provider = MagicMock()
    provider.default_model = default_model
    provider.chat = AsyncMock(return_value=ChatChunk(content=raw_response))
    return provider


def _make_concepts(*names: str) -> list[ConceptItem]:
    items: list[ConceptItem] = []
    for i, n in enumerate(names):
        items.append(
            ConceptItem(
                id=f"cpt-{i:03d}",
                name=n,
                summary=f"{n} 的一句话定义",
                relevance=1.0 - i * 0.05,
            )
        )
    return items


class TestTeachQuestionGeneratorParsing:
    @pytest.mark.asyncio
    async def test_happy_path_builds_sa_questions(self):
        concepts = _make_concepts("Attention", "Softmax")
        provider = _make_provider(
            '[{"concept_id": "cpt-000", "stem": "用自己的话解释 attention",'
            ' "rubric": "必须提到 query/key/value"},'
            '{"concept_id": "cpt-001", "stem": "softmax 的作用是？",'
            ' "rubric": "必须提到归一化为概率"}]'
        )
        gen = TeachQuestionGenerator(provider)

        questions = await gen.generate(
            objective_text="理解 Transformer 注意力",
            concepts=concepts,
        )

        assert len(questions) == 2
        assert all(isinstance(q, TeachQuestion) for q in questions)
        assert all(q.kind == "sa" for q in questions)
        assert {q.concept_id for q in questions} == {"cpt-000", "cpt-001"}
        assert questions[0].stem.startswith("用自己的话")
        assert "query" in questions[0].rubric

    @pytest.mark.asyncio
    async def test_unknown_concept_id_is_dropped(self):
        concepts = _make_concepts("Attention")
        provider = _make_provider(
            '[{"concept_id": "cpt-000", "stem": "ok", "rubric": "r1"},'
            '{"concept_id": "cpt-999", "stem": "bogus", "rubric": "r2"}]'
        )
        gen = TeachQuestionGenerator(provider)

        questions = await gen.generate(
            objective_text="t", concepts=concepts
        )
        assert len(questions) == 1
        assert questions[0].concept_id == "cpt-000"

    @pytest.mark.asyncio
    async def test_duplicate_concept_ids_keep_first(self):
        concepts = _make_concepts("Attention", "Softmax")
        provider = _make_provider(
            '[{"concept_id": "cpt-000", "stem": "v1", "rubric": "r"},'
            '{"concept_id": "cpt-000", "stem": "v2", "rubric": "r"},'
            '{"concept_id": "cpt-001", "stem": "v3", "rubric": "r"}]'
        )
        gen = TeachQuestionGenerator(provider)

        questions = await gen.generate(
            objective_text="t", concepts=concepts
        )
        assert [q.concept_id for q in questions] == ["cpt-000", "cpt-001"]
        assert questions[0].stem == "v1"

    @pytest.mark.asyncio
    async def test_missing_required_fields_are_skipped(self):
        concepts = _make_concepts("A", "B", "C")
        provider = _make_provider(
            '[{"concept_id": "", "stem": "s", "rubric": "r"},'
            '{"concept_id": "cpt-000", "stem": "", "rubric": "r"},'
            '{"concept_id": "cpt-001", "stem": "s", "rubric": ""},'
            '{"concept_id": "cpt-002", "stem": "good", "rubric": "ok"}]'
        )
        gen = TeachQuestionGenerator(provider)

        questions = await gen.generate(
            objective_text="t", concepts=concepts
        )
        assert len(questions) == 1
        assert questions[0].concept_id == "cpt-002"

    @pytest.mark.asyncio
    async def test_max_questions_truncates(self):
        concepts = _make_concepts("A", "B", "C", "D", "E")
        provider = _make_provider(
            '[{"concept_id": "cpt-000", "stem": "s", "rubric": "r"},'
            '{"concept_id": "cpt-001", "stem": "s", "rubric": "r"},'
            '{"concept_id": "cpt-002", "stem": "s", "rubric": "r"},'
            '{"concept_id": "cpt-003", "stem": "s", "rubric": "r"}]'
        )
        gen = TeachQuestionGenerator(provider, max_questions=2)

        questions = await gen.generate(
            objective_text="t", concepts=concepts
        )
        assert len(questions) == 2

    @pytest.mark.asyncio
    async def test_hard_cap_at_session_max(self):
        # 即便用户传一个超大的 max_questions，也被夹到 MAX_QUESTIONS_PER_SESSION
        gen = TeachQuestionGenerator(MagicMock(), max_questions=999)
        assert gen.max_questions == MAX_QUESTIONS_PER_SESSION

    @pytest.mark.asyncio
    async def test_parse_failure_returns_empty(self):
        concepts = _make_concepts("A")
        provider = _make_provider("这不是 JSON")
        gen = TeachQuestionGenerator(provider)

        assert await gen.generate(objective_text="t", concepts=concepts) == []

    @pytest.mark.asyncio
    async def test_non_array_root_returns_empty(self):
        concepts = _make_concepts("A")
        provider = _make_provider('{"concept_id": "cpt-000"}')
        gen = TeachQuestionGenerator(provider)

        assert await gen.generate(objective_text="t", concepts=concepts) == []

    @pytest.mark.asyncio
    async def test_markdown_fence_is_tolerated(self):
        concepts = _make_concepts("Attention")
        provider = _make_provider(
            "好的，结果是：\n"
            "```json\n"
            '[{"concept_id": "cpt-000", "stem": "s", "rubric": "r"}]\n'
            "```"
        )
        gen = TeachQuestionGenerator(provider)

        questions = await gen.generate(objective_text="t", concepts=concepts)
        assert len(questions) == 1


class TestTeachQuestionGeneratorShortCircuit:
    @pytest.mark.asyncio
    async def test_empty_concepts_short_circuits(self):
        provider = _make_provider("[]")
        gen = TeachQuestionGenerator(provider)

        questions = await gen.generate(objective_text="t", concepts=[])
        assert questions == []
        provider.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_exception_returns_empty(self):
        concepts = _make_concepts("A")
        provider = MagicMock()
        provider.default_model = "kimi-2.6"
        provider.chat = AsyncMock(side_effect=RuntimeError("provider down"))
        gen = TeachQuestionGenerator(provider)

        assert await gen.generate(objective_text="t", concepts=concepts) == []


class TestTeachQuestionGeneratorModelRouting:
    @pytest.mark.asyncio
    async def test_explicit_model_name_is_passed_to_provider(self):
        concepts = _make_concepts("A")
        provider = _make_provider("[]")
        gen = TeachQuestionGenerator(
            provider, model_name="claude-haiku-4-5-20251001"
        )

        await gen.generate(objective_text="t", concepts=concepts)

        provider.chat.assert_awaited_once()
        (params,), _ = provider.chat.call_args
        assert isinstance(params, ChatParams)
        assert params.model == "claude-haiku-4-5-20251001"
        assert params.stream is False

    @pytest.mark.asyncio
    async def test_model_name_none_falls_back_to_provider_default(self):
        concepts = _make_concepts("A")
        provider = _make_provider("[]", default_model="kimi-2.6")
        gen = TeachQuestionGenerator(provider, model_name=None)

        await gen.generate(objective_text="t", concepts=concepts)

        (params,), _ = provider.chat.call_args
        assert params.model == "kimi-2.6"

    @pytest.mark.asyncio
    async def test_no_model_resolved_skips_provider_call(self):
        concepts = _make_concepts("A")
        provider = _make_provider("[]", default_model=None)
        gen = TeachQuestionGenerator(provider, model_name=None)

        questions = await gen.generate(objective_text="t", concepts=concepts)
        assert questions == []
        provider.chat.assert_not_awaited()
