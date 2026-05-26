"""ConceptExtractor 单元测试。

覆盖：
- 调用 provider.chat 时把 model_name 透传（用户配置的 cheap-model 走路由库）
- model_name=None 时回退到 provider.default_model
- relevance 分流：>= threshold 进 concept_list，< threshold 进 tangent_notes
- name case-insensitive 去重：与 existing 撞 / 同批内重复都跳过
- 解析失败（非 JSON / 非数组 / 字段缺失）→ 返回 ([], []) 不抛
- markdown 围栏 / 前后噪音容忍：能从中间挖出 [...] 数组
- assistant_text 为空 → 直接短路返回 ([], []) 不调 provider
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


from learning_agent.ai.learning_unit import ConceptItem, TangentNote
from learning_agent.ai.models import ChatChunk, ChatParams
from learning_agent.learning_agent.concept_extractor import ConceptExtractor


def _make_provider(
    raw_response: str,
    *,
    default_model: str | None = "kimi-2.6",
) -> MagicMock:
    provider = MagicMock()
    provider.default_model = default_model
    provider.chat = AsyncMock(return_value=ChatChunk(content=raw_response))
    return provider


class TestConceptExtractorParsing:
    @pytest.mark.asyncio
    async def test_relevance_splits_into_concept_and_tangent(self):
        provider = _make_provider(
            '[{"name": "Attention", "summary": "权重重分配", '
            '"examples": ["softmax"], "relevance": 0.85}, '
            '{"name": "Batch Norm", "summary": "批标准化", '
            '"examples": [], "relevance": 0.3}]'
        )
        extractor = ConceptExtractor(provider, threshold=0.6)

        concepts, tangents = await extractor.extract(
            objective_text="理解 Transformer",
            assistant_text="attention 是...",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )

        assert len(concepts) == 1
        assert concepts[0].name == "Attention"
        assert concepts[0].relevance == pytest.approx(0.85)
        assert concepts[0].examples == ["softmax"]
        assert isinstance(concepts[0], ConceptItem)

        assert len(tangents) == 1
        assert tangents[0].name == "Batch Norm"
        assert tangents[0].relevance == pytest.approx(0.3)
        assert isinstance(tangents[0], TangentNote)

    @pytest.mark.asyncio
    async def test_dedup_against_existing_names_case_insensitive(self):
        provider = _make_provider(
            '[{"name": "Attention", "summary": "新写法", '
            '"examples": [], "relevance": 0.9}]'
        )
        extractor = ConceptExtractor(provider)

        concepts, _ = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            # existing 用小写存（仿 store 真实调用方），输入用首字母大写
            existing_concept_names={"attention"},
            existing_tangent_names=set(),
        )
        assert concepts == []

    @pytest.mark.asyncio
    async def test_dedup_within_batch_keeps_first(self):
        provider = _make_provider(
            '[{"name": "Attention", "summary": "v1", '
            '"examples": [], "relevance": 0.9},'
            '{"name": "ATTENTION", "summary": "v2", '
            '"examples": [], "relevance": 0.95}]'
        )
        extractor = ConceptExtractor(provider)

        concepts, _ = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert len(concepts) == 1
        assert concepts[0].summary == "v1"

    @pytest.mark.asyncio
    async def test_parse_failure_returns_empty_tuple(self):
        provider = _make_provider("这不是 JSON，模型摆烂了")
        extractor = ConceptExtractor(provider)

        concepts, tangents = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert concepts == []
        assert tangents == []

    @pytest.mark.asyncio
    async def test_non_array_root_returns_empty(self):
        provider = _make_provider('{"name": "Attention"}')
        extractor = ConceptExtractor(provider)

        concepts, tangents = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert concepts == []
        assert tangents == []

    @pytest.mark.asyncio
    async def test_markdown_fence_is_tolerated(self):
        provider = _make_provider(
            "好的，结果是：\n"
            "```json\n"
            '[{"name": "Attention", "summary": "s", "examples": [], '
            '"relevance": 0.9}]\n'
            "```"
        )
        extractor = ConceptExtractor(provider)

        concepts, _ = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert len(concepts) == 1
        assert concepts[0].name == "Attention"

    @pytest.mark.asyncio
    async def test_missing_required_fields_are_skipped(self):
        provider = _make_provider(
            '[{"name": "", "summary": "no name", "relevance": 0.9},'
            '{"name": "ok", "summary": "", "relevance": 0.9},'
            '{"name": "good", "summary": "g", "relevance": 0.9}]'
        )
        extractor = ConceptExtractor(provider)

        concepts, _ = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert len(concepts) == 1
        assert concepts[0].name == "good"

    @pytest.mark.asyncio
    async def test_examples_capped_at_two(self):
        provider = _make_provider(
            '[{"name": "X", "summary": "s", '
            '"examples": ["a", "b", "c", "d"], "relevance": 0.9}]'
        )
        extractor = ConceptExtractor(provider)

        concepts, _ = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert concepts[0].examples == ["a", "b"]

    @pytest.mark.asyncio
    async def test_relevance_clamped_to_unit_interval(self):
        provider = _make_provider(
            '[{"name": "A", "summary": "s", "relevance": 1.5},'
            '{"name": "B", "summary": "s", "relevance": -0.3}]'
        )
        extractor = ConceptExtractor(provider, threshold=0.6)

        concepts, tangents = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        # 1.5 被夹到 1.0 进 concepts；-0.3 被夹到 0.0 进 tangents
        assert len(concepts) == 1 and concepts[0].relevance == pytest.approx(1.0)
        assert len(tangents) == 1 and tangents[0].relevance == pytest.approx(0.0)


class TestConceptExtractorModelRouting:
    @pytest.mark.asyncio
    async def test_explicit_model_name_is_passed_to_provider(self):
        provider = _make_provider("[]")
        extractor = ConceptExtractor(
            provider, model_name="claude-haiku-4-5-20251001"
        )

        await extractor.extract(
            objective_text="t",
            assistant_text="hello",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )

        provider.chat.assert_awaited_once()
        (params,), _ = provider.chat.call_args
        assert isinstance(params, ChatParams)
        assert params.model == "claude-haiku-4-5-20251001"
        assert params.stream is False

    @pytest.mark.asyncio
    async def test_model_name_none_falls_back_to_provider_default(self):
        provider = _make_provider("[]", default_model="kimi-2.6")
        extractor = ConceptExtractor(provider, model_name=None)

        await extractor.extract(
            objective_text="t",
            assistant_text="hello",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )

        (params,), _ = provider.chat.call_args
        assert params.model == "kimi-2.6"

    @pytest.mark.asyncio
    async def test_no_model_resolved_skips_provider_call(self):
        provider = _make_provider("[]", default_model=None)
        extractor = ConceptExtractor(provider, model_name=None)

        concepts, tangents = await extractor.extract(
            objective_text="t",
            assistant_text="hello",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )

        assert concepts == [] and tangents == []
        provider.chat.assert_not_awaited()


class TestConceptExtractorShortCircuit:
    @pytest.mark.asyncio
    async def test_empty_assistant_text_skips_provider_call(self):
        provider = _make_provider("[]")
        extractor = ConceptExtractor(provider)

        concepts, tangents = await extractor.extract(
            objective_text="t",
            assistant_text="   ",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )

        assert concepts == [] and tangents == []
        provider.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_exception_returns_empty_tuple(self):
        provider = MagicMock()
        provider.default_model = "kimi-2.6"
        provider.chat = AsyncMock(side_effect=RuntimeError("provider down"))
        extractor = ConceptExtractor(provider)

        concepts, tangents = await extractor.extract(
            objective_text="t",
            assistant_text="x",
            existing_concept_names=set(),
            existing_tangent_names=set(),
        )
        assert concepts == [] and tangents == []
