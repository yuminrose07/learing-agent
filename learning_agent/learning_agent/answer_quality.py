"""Final Answer Guarantee：答案可用性判定与兜底救援。

从 ``LearningAgentSystem`` 抽出的答案质量逻辑层：

- ``classify_incomplete_answer`` / ``final_answer_verdict``：纯函数，判定本轮已
  流式输出的可见答案是否需要 rescue 收口。
- ``build_rescue_answer``：判定需要兜底时，用 provider 生成一个不依赖工具的中性
  回答；provider 缺失或调用失败则回退到静态兜底文本。provider 由调用方注入。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from learning_agent.ai import ChatMessage, ChatParams, MessageRole

if TYPE_CHECKING:
    from learning_agent.ai.openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


# Final Answer Guarantee 的最终静态兜底文本。仅在 rescue LLM 也失败时使用，
# 不可包含任何"错误 / 重试 / 失败"字样。保持中性，让用户可以继续对话。
SAFE_FALLBACK_ANSWER = (
    "我这边暂时没有抓到你需要的具体信息——能再告诉我一些上下文吗？"
    "比如你想了解的方向、目标场景，或者你已经看过的资料，我会基于这些继续帮你梳理。"
)


# 视为"承诺下文却没有下文"的悬挂结尾标点。正常答案极少以这些字符收尾。
DANGLING_TAILS = ("：", ":", "，", ",", "、", "；", ";", "…", "—", "－", "-")
# 残句判定的长度上限：超过则认为是正常长答案，不因结尾标点误判。
INCOMPLETE_MAX_LEN = 48
# 流错误后"部分片段"判定的长度上限：短于此且本轮发生过系统级流错误，视为被截断。
PARTIAL_AFTER_ERROR_MAX_LEN = 120


def classify_incomplete_answer(visible_text: str) -> Optional[str]:
    """对已流式输出的可见答案做"是否是可用答案"判定。

    返回非 None 的 reason_code 表示判定为"对用户不可用"、需要 rescue 收口；
    返回 None 表示是可用答案。

    保守策略：只命中高置信信号，避免误伤正常答案。
    - 空：完全没有可见内容。
    - 残句：很短且以悬挂标点（冒号/逗号/顿号/破折号等）收尾，例如
      "让我尝试其他来源："——模型承诺下文却以无工具调用的纯文本收场，
      ReAct 循环把它当成最终答案提前终止。
    """
    stripped = visible_text.strip()
    if not stripped:
        return "empty_stream"
    if len(stripped) <= INCOMPLETE_MAX_LEN and stripped[-1] in DANGLING_TAILS:
        return "incomplete_answer"
    return None


def final_answer_verdict(
    *,
    visible_text: str,
    stream_error_reason: Optional[str],
    inner_reason: Optional[str],
) -> Optional[str]:
    """综合判定本轮是否需要 rescue 收口，返回 reason_code 或 None（可用答案）。

    判定顺序（保守优先）：
    1. 完全无可见输出（含被抑制的系统错误 chunk / inner 异常）→ 必然 rescue。
    2. 有可见输出但是残句/截断 → rescue。
    3. 有可见输出、本轮发生过系统级流错误、且可见内容很短（疑似被截断的片段）→ rescue。
    4. 其余视为可用答案，不 rescue。

    第 3 条带长度上限，避免把"完整长答案 + 末尾一次延迟流错误"误判成需要兜底。
    """
    stripped = visible_text.strip()
    if not stripped:
        return stream_error_reason or inner_reason or "empty_stream"
    incomplete = classify_incomplete_answer(stripped)
    if incomplete:
        return incomplete
    if stream_error_reason is not None and len(stripped) <= PARTIAL_AFTER_ERROR_MAX_LEN:
        return stream_error_reason
    return None


async def build_rescue_answer(
    provider: Optional["OpenAIProvider"],
    *,
    session_id: str,
    user_input: str,
    reason: str,
    detail: str,
) -> tuple[str, str]:
    """生成最终兜底回答。不允许把内部错误暴露给用户。

    优先：让 provider 基于 user_input 直接给一个不依赖工具的中性回答。
    次级：返回一段中性静态文本，仍保证 content 非空。
    """
    if provider is not None:
        try:
            prompt = (
                "You are answering a user in a personal learning assistant. "
                "Earlier internal tools or data lookups did not yield a usable answer, "
                "but the user must receive a helpful, self-contained reply. "
                "Do NOT mention any internal tools, errors, retries, or system state. "
                "Do NOT apologize for technical issues. "
                "If you genuinely need more information, ask one concise clarifying question. "
                "Otherwise, give a useful answer based on general knowledge. "
                "Match the user's language (Chinese or English) automatically.\n\n"
                f"User message:\n{user_input}"
            )
            chunk = await provider.chat(
                ChatParams(
                    model=provider.default_model,
                    messages=[
                        ChatMessage(role=MessageRole.USER, content=prompt),
                    ],
                    temperature=0.4,
                    stream=False,
                    max_tokens=800,
                )
            )
            text = (chunk.content or "").strip()
            if text:
                return text, "rescue_llm"
        except Exception:
            logger.exception(
                "[System] rescue LLM call failed for session %s (reason=%s)",
                session_id, reason,
            )
    return SAFE_FALLBACK_ANSWER, "static_fallback"
