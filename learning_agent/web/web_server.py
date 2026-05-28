"""
Learning-Agent Web API（FastAPI）。

提供 HTTP 接口供外部调用，支持：
- 创建/查询学习目标与会话
- 流式/非流式对话（SSE）
- 记忆状态查询
- 会话 Fork、知识确认等

启动方式：
    uvicorn learning_agent.web.web_server:app --reload --port 8000
或直接：
    python -m learning_agent.learning_agent.main --web [--port 8000]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from learning_agent.ai import AgentMode
from learning_agent.learning_agent.config import Config
from learning_agent.learning_agent.companion_policy import (
    CompanionAdviceLevel,
    CompanionStyle,
    build_companion_settings_payload,
    companion_metadata_for_update,
    companion_style_options,
    normalize_advice_level,
    normalize_companion_style,
)
from learning_agent.learning_agent.learning_unit_store import ActiveUnitExistsError
from learning_agent.learning_agent.learning_unit_metrics import summary_to_dict
from learning_agent.learning_agent.main import LearningAgentSystem, SessionNotFoundError
from learning_agent.learning_agent.mode_service import (
    NEUTRAL_PERSONA,
    PHILOSOPHER_PERSONAS,
    resolve_persona,
)
from learning_agent.learning_agent.session_event_store import filter_events
from learning_agent.learning_agent.session_events import SessionEventType
from learning_agent.web.eval_routes import router as eval_router

logger = logging.getLogger(__name__)

# SSE 心跳间隔：静默期内每隔多少秒发一行 SSE 注释（": ping"），
# 用于穿透浏览器/反向代理的空闲超时（典型默认 30s/60s）。
_SSE_HEARTBEAT_SECONDS = 15.0

# ───────────────────────────────
# 请求/响应模型
# ───────────────────────────────

class CreateObjectiveRequest(BaseModel):
    title: str
    description: Optional[str] = None


class CreateSessionRequest(BaseModel):
    objective_id: Optional[str] = None
    title: Optional[str] = "Web Session"
    mode_metadata: Optional[dict[str, Any]] = None


class ChatRequest(BaseModel):
    message: str
    stream: bool = True
    mode: AgentMode = AgentMode.CHAT


class ConfirmKnowledgeRequest(BaseModel):
    node_id: str


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = None


class UpdatePersonaRequest(BaseModel):
    # ``None`` / empty / "neutral" all mean "no overlay" (default).
    persona_key: Optional[str] = None


class UpdateCompanionRequest(BaseModel):
    enabled: bool = True
    style: Optional[str] = None
    advice_level: Optional[str] = None


class CreateLearningUnitRequest(BaseModel):
    seed_text: str
    source: Literal[
        "ai_distilled",
        "user_written",
        "material_imported",
    ] = "ai_distilled"
    mode_metadata: Optional[dict[str, Any]] = None


class AdvanceLearningUnitRequest(BaseModel):
    target_phase: Literal["absorbing", "outputting", "consolidated"]


class RefineObjectiveRequest(BaseModel):
    new_text: str


class ReuseFeedbackRequest(BaseModel):
    value: str  # "yes" | "no"


class StopLearningUnitRequest(BaseModel):
    reason: str = "user_stopped"


# ───────────────────────────────
# 生命周期 & 全局系统实例
# ───────────────────────────────

_system: Optional[LearningAgentSystem] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _system
    config = Config(config_path=os.getenv("LA_CONFIG_PATH"))
    _system = LearningAgentSystem(config)
    await _system.initialize()

    # 启动时清理残留的运行时（防御性措施，防止热重载等场景下旧状态残留）
    _system.clear_all_runtimes()
    logger.info("[Web] System initialized. All session runtimes cleared.")

    yield

    # 关闭时清理所有运行时
    _system.clear_all_runtimes()
    await _system.shutdown()
    logger.info("[Web] System shutdown.")


app = FastAPI(
    title="Learning-Agent API",
    version="0.4.0-alpha.1",
    lifespan=lifespan,
)

# 允许前端跨域调用（包括 file:// 协议）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# E2E 评测控制台路由（/api/eval/*）。独立 router,与现有 /sessions /chat
# /learning-units 等业务路由完全隔离。
app.include_router(eval_router)


# ───────────────────────────────
# 辅助函数
# ───────────────────────────────

def _get_system() -> LearningAgentSystem:
    if _system is None:
        raise HTTPException(status_code=503, detail="System not initialized")
    return _system


async def _stream_chat_chunks(
    system: LearningAgentSystem,
    session_id: str,
    message: str,
    mode: AgentMode = AgentMode.CHAT,
) -> AsyncGenerator[str, None]:
    """将产品层对话流转换为 SSE 格式，并在静默期发心跳防止超时断流。

    上游 ``stream_session_chat`` 会先做一次同步重的准备
    （compaction plan / 首 token 预热），期间没有数据下行。
    用 ``asyncio.Queue`` 把生产与消费解耦，消费侧每
    ``_SSE_HEARTBEAT_SECONDS`` 秒没拿到新数据就发一行 SSE 注释
    （前端 parser 会忽略 ``:`` 开头的行），避免被反向代理或浏览器
    判定为空闲连接而 RST。
    """
    # 立刻送一行注释，触发 Starlette 把响应头 flush 出去，
    # 使前端的 fetch().response 尽早 resolve，避免 TTFB 阶段被代理切断。
    yield ": stream-open\n\n"

    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for chunk in system.stream_session_chat(session_id, message, mode=mode):
                chunk_metadata = dict(chunk.metadata)
                payload = {
                    "content": chunk.content,
                    "tool_call": chunk.tool_call,
                    "finish_reason": chunk.finish_reason,
                    "mode": chunk_metadata.get("mode", mode.value),
                    "alignment": chunk_metadata.get("alignment", mode == AgentMode.ASK),
                    "persona_key": chunk_metadata.get("persona_key"),
                    "persona_name": chunk_metadata.get("persona_name"),
                    "persona_role": chunk_metadata.get("persona_role"),
                    "usage": chunk_metadata.get("usage") or chunk_metadata.get("turn_usage"),
                }
                # adaptive alignment §11.3：学习卷专属元数据，前端按此驱动
                # 目标卡片 + 收窄建议条 + 验收按钮，避免轮询 GET /learning-units/{id}。
                for key in (
                    "learning_unit_id",
                    "learning_unit_phase",
                    "alignment_state",
                    "objective_status",
                    "alignment_reason",
                    "assumption_note",
                    "suggested_objective",
                    "candidates",
                    "divergence_cost",
                    "placeholder_text",
                    "alignment_popup",
                    "teach_session_id",
                    "teach_state",
                    "question_index",
                    "question_total",
                    "verdict",
                    "feedback_card",
                    "forge_stage",
                    "temperature_state",
                    "learning_action",
                    "chat_profile",
                    "companion_enabled",
                    "companion_style",
                    "companion_style_name",
                    "companion_intent",
                    "companion_advice_level",
                    "stress_relief",
                ):
                    value = chunk_metadata.get(key)
                    if value is not None:
                        payload[key] = value
                # Final Answer Guarantee 标记：让前端可以选择性渲染（仍是正常 content）
                if chunk_metadata.get("rescue"):
                    payload["rescue"] = True
                await queue.put(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
        except SessionNotFoundError:
            await queue.put(
                f"data: {json.dumps({'error': f'Session {session_id} not found'}, ensure_ascii=False)}\n\n"
            )
        except Exception as e:
            # 工具调用成功导向架构：理论上 stream_session_chat 已经自带兜底，
            # 这里只可能命中 BaseException 子类或上游初始化期错误（如 system 未启动）。
            # 仍然不能让前端拿到无 content 的流，输出一条静态兜底文本。
            logger.exception(f"[Web] Chat stream outer-guard error: {e}")
            try:
                await system.unresolved_failure_logger.record(
                    session_id=session_id,
                    layer="web_server_stream_outer",
                    reason_code=type(e).__name__,
                    message=str(e),
                    user_input=message,
                    rescue_used=True,
                    extra={"mode": mode.value},
                )
            except Exception:
                logger.exception("[Web] unresolved_failure_logger.record failed (non-fatal)")
            fallback_payload = {
                "content": (
                    "我这边暂时没办法完成这条请求——你能再描述一次或换个角度问我吗？"
                ),
                "rescue": True,
                "rescue_reason": "web_outer_exception",
                "mode": mode.value,
            }
            await queue.put(
                f"data: {json.dumps(fallback_payload, ensure_ascii=False)}\n\n"
            )
        finally:
            await queue.put(None)

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield item
        yield "data: [DONE]\n\n"
    finally:
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except (asyncio.CancelledError, Exception):
                pass


# ───────────────────────────────
# 健康检查
# ───────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "0.4.0-alpha.1"}


# ───────────────────────────────
# 学习目标
# ───────────────────────────────

@app.post("/objectives")
async def create_objective(req: CreateObjectiveRequest) -> dict[str, Any]:
    system = _get_system()
    obj = await system.create_objective(req.title, req.description)
    return obj.model_dump()


@app.get("/objectives")
async def list_objectives() -> list[dict[str, Any]]:
    system = _get_system()
    return [objective.model_dump() for objective in system.list_objectives()]


@app.get("/objectives/{objective_id}")
async def get_objective(objective_id: str) -> dict[str, Any]:
    system = _get_system()
    objective = system.get_objective(objective_id)
    if objective is None:
        raise HTTPException(status_code=404, detail="Objective not found")
    return objective.model_dump()


# ───────────────────────────────
# 会话
# ───────────────────────────────

@app.post("/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    system = _get_system()
    session = system.create_session(
        objective_id=req.objective_id,
        title=req.title,
    )
    if req.mode_metadata:
        session.mode_metadata.update(req.mode_metadata)
        manager = getattr(system, "session_manager", None)
        persist = getattr(manager, "persist_mode_metadata", None)
        if callable(persist):
            persist(session.id)
    data = session.model_dump(exclude={"entries"})
    data["messages"] = []
    return data


@app.get("/sessions")
async def list_sessions(include_eval: bool = False) -> list[dict[str, Any]]:
    system = _get_system()
    sessions = system.list_sessions()
    if not include_eval:
        sessions = [
            session
            for session in sessions
            if session.mode_metadata.get("source") != "eval"
        ]
    return [session.model_dump(exclude={"entries"}) for session in sessions]


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    system = _get_system()
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    data = session.model_dump(exclude={"entries"})
    data["messages"] = system.get_ui_messages(session_id)
    return data


# ───────────────────────────────
# 对话
# ───────────────────────────────

@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest) -> Any:
    system = _get_system()
    if req.stream:
        return StreamingResponse(
            _stream_chat_chunks(system, session_id, req.message, mode=req.mode),
            media_type="text/event-stream",
            headers={
                # 禁掉所有可能的缓冲/转换：浏览器缓存、nginx proxy_buffering、
                # 任何会改 body 的中间件（如 gzip 重分块）。
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    else:
        try:
            content = await system.collect_session_chat(
                session_id,
                req.message,
                mode=req.mode,
            )
        except SessionNotFoundError:
            raise HTTPException(status_code=404, detail="Session not found")
        except Exception as e:
            # 工具调用成功导向架构：collect_session_chat 自带兜底，
            # 这里只可能命中初始化期或框架级错误。仍然不向用户暴露原始错误信息。
            logger.exception(f"[Web] Chat error: {e}")
            try:
                await system.unresolved_failure_logger.record(
                    session_id=session_id,
                    layer="web_server_collect_outer",
                    reason_code=type(e).__name__,
                    message=str(e),
                    user_input=req.message,
                    rescue_used=True,
                    extra={"mode": req.mode.value},
                )
            except Exception:
                logger.exception("[Web] unresolved_failure_logger.record failed (non-fatal)")
            content = (
                "我这边暂时没办法完成这条请求——你能再描述一次或换个角度问我吗？"
            )
        return {
            "session_id": session_id,
            "content": content,
        }


# ───────────────────────────────
# 学伴风格(persona / 思路)
# ───────────────────────────────

def _persona_payload(persona) -> dict[str, Any]:
    return {
        "key": persona.key,
        "display_name": persona.display_name,
        "role_name": persona.role_name,
        "home_mode": persona.mode.value,
    }


@app.get("/personas")
async def list_personas() -> dict[str, Any]:
    """Return the neutral default plus the curated philosopher overlays."""
    return {
        "default": _persona_payload(NEUTRAL_PERSONA),
        "philosophers": [_persona_payload(p) for p in PHILOSOPHER_PERSONAS],
    }


@app.get("/sessions/{session_id}/persona")
async def get_session_persona(session_id: str) -> dict[str, Any]:
    system = _get_system()
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    stored = session.mode_metadata.get("chat_persona_key")
    persona = resolve_persona(session.mode, persona_key=stored if isinstance(stored, str) else None)
    return {
        "session_id": session.id,
        "persona_key": persona.key,
        "display_name": persona.display_name,
        "role_name": persona.role_name,
    }


@app.put("/sessions/{session_id}/persona")
async def update_session_persona(session_id: str, req: UpdatePersonaRequest) -> dict[str, Any]:
    system = _get_system()
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    requested = (req.persona_key or "").strip() or None
    if requested is None or requested == NEUTRAL_PERSONA.key:
        session.mode_metadata.pop("chat_persona_key", None)
        persona = NEUTRAL_PERSONA
    else:
        persona = resolve_persona(session.mode, persona_key=requested)
        if persona.key == NEUTRAL_PERSONA.key and requested != NEUTRAL_PERSONA.key:
            raise HTTPException(status_code=400, detail=f"Unknown persona: {requested}")
        session.mode_metadata["chat_persona_key"] = persona.key

    if hasattr(system, "session_manager"):
        system.session_manager.persist_mode_metadata(session.id)

    return {
        "session_id": session.id,
        "persona_key": persona.key,
        "display_name": persona.display_name,
        "role_name": persona.role_name,
    }


# ───────────────────────────────
# 闲聊陪伴档案(companion / 减压)
# ───────────────────────────────

@app.get("/companion-styles")
async def list_companion_styles() -> dict[str, Any]:
    return {
        "styles": companion_style_options(),
        "default_style": CompanionStyle.WARM_GIRLFRIEND.value,
        "default_advice_level": CompanionAdviceLevel.LOW.value,
    }


@app.get("/sessions/{session_id}/companion")
async def get_session_companion(session_id: str) -> dict[str, Any]:
    system = _get_system()
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return build_companion_settings_payload(session)


@app.put("/sessions/{session_id}/companion")
async def update_session_companion(
    session_id: str,
    req: UpdateCompanionRequest,
) -> dict[str, Any]:
    system = _get_system()
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    requested_style = (req.style or "").strip() or None
    if requested_style is not None:
        style = normalize_companion_style(requested_style)
        if style == CompanionStyle.OFF and requested_style != CompanionStyle.OFF.value:
            raise HTTPException(status_code=400, detail=f"Unknown companion style: {requested_style}")

    requested_advice = (req.advice_level or "").strip() or None
    if requested_advice is not None:
        advice = normalize_advice_level(requested_advice)
        if advice.value != requested_advice:
            raise HTTPException(status_code=400, detail=f"Unknown companion advice level: {requested_advice}")

    effective_style = requested_style
    if req.enabled and effective_style is None:
        effective_style = (
            session.mode_metadata.get("companion_style")
            or CompanionStyle.WARM_GIRLFRIEND.value
        )
    effective_advice = requested_advice
    if effective_advice is None:
        effective_advice = session.mode_metadata.get("companion_advice_level")

    updates = companion_metadata_for_update(
        enabled=req.enabled,
        style=effective_style,
        advice_level=effective_advice,
    )
    changed: dict[str, Any] = {}
    for key, value in updates.items():
        if value is None:
            if key in session.mode_metadata:
                session.mode_metadata.pop(key, None)
                changed[key] = None
        elif session.mode_metadata.get(key) != value:
            session.mode_metadata[key] = value
            changed[key] = value

    if hasattr(system, "session_manager"):
        system.session_manager.persist_mode_metadata(session.id)
    if changed:
        system._emit_companion_event(
            session,
            SessionEventType.COMPANION_PROFILE_CHANGED,
            {
                "enabled": req.enabled,
                "style": (
                    session.mode_metadata.get("companion_style")
                    or CompanionStyle.OFF.value
                ),
                "advice_level": session.mode_metadata.get("companion_advice_level")
                or CompanionAdviceLevel.LOW.value,
                "metadata_changed": changed,
                "source": "api",
            },
        )
    return build_companion_settings_payload(session)


# ───────────────────────────────
# 会话更新与删除
# ───────────────────────────────

@app.put("/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> dict[str, Any]:
    system = _get_system()
    session = system.update_session_title(session_id, req.title)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    data = session.model_dump(exclude={"entries"})
    data["messages"] = system.get_ui_messages(session_id)
    return data


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    system = _get_system()
    deleted = await system.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"status": "deleted", "session_id": session_id}


# ───────────────────────────────
# 学习卷（LearningUnit）
# ───────────────────────────────

def _learning_unit_payload(unit, *, session_id: Optional[str] = None) -> dict[str, Any]:
    data = unit.model_dump(mode="json")
    if session_id is not None:
        data["session_id"] = session_id
    return data


@app.post("/learning-units")
async def create_learning_unit(req: CreateLearningUnitRequest) -> dict[str, Any]:
    system = _get_system()
    try:
        kwargs: dict[str, Any] = {
            "seed_text": req.seed_text,
            "source": req.source,
        }
        if req.mode_metadata:
            kwargs["mode_metadata"] = req.mode_metadata
        session, unit = system.create_learning_unit(**kwargs)
    except ActiveUnitExistsError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "An active learning unit already exists; close it first.",
                "active_unit_id": exc.active_unit_id,
            },
        )
    return _learning_unit_payload(unit, session_id=session.id)


@app.get("/learning-units")
async def list_learning_units() -> list[dict[str, Any]]:
    system = _get_system()
    return [_learning_unit_payload(unit) for unit in system.list_learning_units()]


@app.get("/learning-units/metrics")
async def get_learning_unit_metrics(
    window_days: Optional[int] = 7,
) -> dict[str, Any]:
    """M2：返回 4 个 P0 指标（TTFV / consolidation / teach-entry / reuse）。

    ``window_days=0`` 表示当天，``None`` 走全量回看（前端可以传 ``window_days=-1``
    或省略该参数）。
    """
    system = _get_system()
    if window_days is not None and window_days < 0:
        window_days = None
    summary = system.get_learning_unit_metrics(window_days=window_days)
    return summary_to_dict(summary)


@app.get("/learning-units/{unit_id}")
async def get_learning_unit(unit_id: str) -> dict[str, Any]:
    system = _get_system()
    unit = system.get_learning_unit(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/confirm-objective")
async def confirm_learning_unit_objective(unit_id: str) -> dict[str, Any]:
    system = _get_system()
    try:
        unit = system.confirm_learning_unit_objective(unit_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/advance")
async def advance_learning_unit(
    unit_id: str, req: AdvanceLearningUnitRequest
) -> dict[str, Any]:
    system = _get_system()
    try:
        unit = await system.advance_learning_unit(unit_id, req.target_phase)
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/stop")
async def stop_learning_unit(
    unit_id: str, req: StopLearningUnitRequest | None = None
) -> dict[str, Any]:
    system = _get_system()
    try:
        unit = system.stop_learning_unit(
            unit_id,
            reason=(req.reason if req is not None else "user_stopped"),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/align")
async def request_learning_unit_alignment(unit_id: str) -> dict[str, Any]:
    """adaptive alignment §6.3：用户主动点"帮我收窄"。"""
    system = _get_system()
    try:
        unit = await system.request_alignment(unit_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/accept-assumption")
async def accept_learning_unit_assumption(unit_id: str) -> dict[str, Any]:
    """adaptive alignment §6.3：用户点"先按这个学"消除建议条。"""
    system = _get_system()
    try:
        unit = await system.accept_assumption(unit_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/refine-objective")
async def refine_learning_unit_objective(
    unit_id: str, req: RefineObjectiveRequest
) -> dict[str, Any]:
    """adaptive alignment §6.3：用户改写工作目标。"""
    system = _get_system()
    try:
        unit = await system.refine_objective(unit_id, req.new_text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


@app.post("/learning-units/{unit_id}/reuse-feedback")
async def record_learning_unit_reuse_feedback(
    unit_id: str, req: ReuseFeedbackRequest
) -> dict[str, Any]:
    """M2：反馈卡上"下次还会用学习模式吗"轻量问卷。

    400：value 非 yes/no，或卷未进入 consolidated。404：unit 不存在。
    """
    system = _get_system()
    try:
        unit = system.record_reuse_feedback(unit_id, req.value)
    except KeyError:
        raise HTTPException(status_code=404, detail="Learning unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _learning_unit_payload(unit)


# ───────────────────────────────
# 记忆
# ───────────────────────────────

@app.get("/memory")
async def get_memory() -> dict[str, Any]:
    system = _get_system()
    return {
        "l1_candidates": [
            node.model_dump() if hasattr(node, "model_dump") else node
            for node in system.memory_manager.get_l1_candidates()
        ],
        "l2_nodes": [
            node.model_dump() if hasattr(node, "model_dump") else node
            for node in system.memory_manager.get_l2_nodes()
        ],
        "l3_nodes": [
            node.model_dump() if hasattr(node, "model_dump") else node
            for node in system.memory_manager.get_l3_nodes()
        ],
        "due_reviews": [
            node.model_dump() if hasattr(node, "model_dump") else node
            for node in system.memory_manager.get_due_reviews()
        ],
    }


# ───────────────────────────────
# 知识确认
# ───────────────────────────────

@app.post("/knowledge/{node_id}/confirm")
async def confirm_knowledge(node_id: str) -> dict[str, str]:
    system = _get_system()
    promoted = await system.confirm_knowledge_candidate(node_id, source="web")
    if promoted is None:
        raise HTTPException(status_code=404, detail="Knowledge candidate not found")
    return {"status": "confirmed", "node_id": node_id}


# ───────────────────────────────
# 状态保存
# ───────────────────────────────

@app.post("/save")
async def save_state() -> dict[str, str]:
    system = _get_system()
    await system.save_state()
    return {"status": "saved"}


# ───────────────────────────────
# 会话事件流（L1 timeline API）
# ───────────────────────────────
#
# 2026-05-24 重构：旧的 /observability/* 端点（errors / flows / traces /
# events / metrics / runtimes / logs / events/stream）已全部下线。它们读的
# .observability/*.jsonl / trace_*.json / flow_*.json 文件不再写入；
# 事件流改由 L1 (<base_dir>/sessions/<id>.events.jsonl) 提供，下方的
# /sessions/{id}/events 端点是新流的唯一入口。
# 详见 docs/design/design-observability-l1-l4-architecture.md §九。


_MAX_EVENT_PAGE = 5000


def _parse_csv_set(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    items = {token.strip() for token in raw.split(",") if token.strip()}
    return items or None


@app.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: str,
    visibility: str | None = None,
    type: str | None = None,
    after_seq: int | None = None,
    limit: int = _MAX_EVENT_PAGE,
) -> dict[str, Any]:
    """读取一份 session 的 L1 事件流（append-only JSONL）。

    Query 参数：
    - ``visibility``: CSV，e.g. ``"agent,system,observability"``，缺省返回全部 visibility
    - ``type``: CSV，e.g. ``"tool.exec_started,tool.exec_completed"``，缺省返回全部类型
    - ``after_seq``: int。**轮询契约**：客户端用 ``after_seq=last_seen_seq`` 实现 tail，
      只拿严格大于该 seq 的事件
    - ``limit``: 单次返回上限（默认 5000，硬上限 5000）

    响应：
        ``{"events": [...SessionEvent.model_dump(mode="json")...], "next_after_seq": <int>}``

    ``next_after_seq`` 是本次返回里最大的 seq；客户端下次调用直接把它原样塞回
    ``after_seq``。**不返回 ``total``**——底层是 append-only JSONL，没法不扫全文件就
    给出总数。

    并发性：``read_session_events`` 与 ``append_event`` 没有共享锁，但单条
    ``append`` 是一次完整 ``json.dumps + "\\n"`` 的 write，正常使用下不会读到半行。
    """
    system = _get_system()
    if system.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")

    visibility_set = _parse_csv_set(visibility)
    types_set = _parse_csv_set(type)
    capped_limit = max(1, min(limit, _MAX_EVENT_PAGE))

    events = system.session_event_store.read_events(session_id)
    filtered = filter_events(
        events,
        visibility=visibility_set,
        types=types_set,
        after_seq=after_seq,
        limit=capped_limit,
    )
    next_after_seq = filtered[-1].seq if filtered else (after_seq or 0)
    return {
        "events": [event.model_dump(mode="json") for event in filtered],
        "next_after_seq": next_after_seq,
    }


@app.post("/sessions/{session_id}/reset-runtime")
async def reset_session_runtime(session_id: str) -> dict[str, Any]:
    """重置指定 session 的运行时状态（不清除聊天记录）。"""
    system = _get_system()
    maintenance_result = system.reset_session_runtime(session_id)
    if maintenance_result is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "status": "runtime_reset",
        "session_id": session_id,
        "runtime": maintenance_result,
    }


# ───────────────────────────────
# 前端静态资源挂载
# ───────────────────────────────
#
# 把项目根的 web/ 目录挂在 /ui，根路径 / 重定向到 /ui/index.html。
# 用 /ui 前缀而非 / 是为了避免与 API 路由冲突；用 RedirectResponse 让
# `http://localhost:8000/` 这个最常用的入口仍然直达聊天页。

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_WEB_DIR)), name="ui")


@app.get("/")
async def _root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/index.html")
