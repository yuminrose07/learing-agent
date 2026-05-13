"""
Learning-Agent Web API（FastAPI）。

提供 HTTP 接口供外部调用，支持：
- 创建/查询学习目标与会话
- 流式/非流式对话（SSE）
- 记忆状态查询
- 会话 Fork、知识确认等

启动方式：
    python -m learning_agent.main --web [--port 8000]
或直接：
    uvicorn learning_agent.web_server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from learning_agent.config import Config
from learning_agent.main import LearningAgentSystem
from learning_agent.models import ChatChunk, Event

logger = logging.getLogger(__name__)

# ───────────────────────────────
# 请求/响应模型
# ───────────────────────────────

class CreateObjectiveRequest(BaseModel):
    title: str
    description: Optional[str] = None


class CreateSessionRequest(BaseModel):
    objective_id: Optional[str] = None
    title: Optional[str] = "Web Session"


class ChatRequest(BaseModel):
    message: str
    stream: bool = True
    ask_mode: bool = False


class ForkRequest(BaseModel):
    entry_id: Optional[str] = None


class ConfirmKnowledgeRequest(BaseModel):
    node_id: str


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = None


class SaveStateRequest(BaseModel):
    pass


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
    _system.agent_loop.clear_all_runtimes()
    logger.info("[Web] System initialized. All session runtimes cleared.")

    yield

    # 关闭时清理所有运行时
    _system.agent_loop.clear_all_runtimes()
    await _system.shutdown()
    logger.info("[Web] System shutdown.")


app = FastAPI(
    title="Learning-Agent API",
    version="0.1.0",
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
    ask_mode: bool = False,
) -> AsyncGenerator[str, None]:
    """将 AgentLoop.run() 的流式输出转换为 SSE 格式。"""
    session = system.session_manager._sessions.get(session_id)
    if session is None:
        yield f"data: {json.dumps({'error': f'Session {session_id} not found'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    try:
        async for chunk in system.agent_loop.run(session, message, ask_mode=ask_mode):
            payload = {
                "content": chunk.content,
                "tool_call": chunk.tool_call,
                "finish_reason": chunk.finish_reason,
                "ask_mode": ask_mode,
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    except Exception as e:
        logger.exception(f"[Web] Chat stream error: {e}")
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    finally:
        # 每次对话结束后自动持久化会话内容
        try:
            system.file_store.save_session(session_id, session.model_dump())
        except Exception:
            logger.exception(f"[Web] Failed to save session {session_id}")
        yield "data: [DONE]\n\n"


# ───────────────────────────────
# 健康检查
# ───────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "0.1.0"}


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
    objectives = []
    for oid in system.file_store.list_objectives():
        data = system.file_store.load_objective(oid)
        if data:
            objectives.append(data)
    return objectives


@app.get("/objectives/{objective_id}")
async def get_objective(objective_id: str) -> dict[str, Any]:
    system = _get_system()
    data = system.file_store.load_objective(objective_id)
    if not data:
        raise HTTPException(status_code=404, detail="Objective not found")
    return data


# ───────────────────────────────
# 会话
# ───────────────────────────────

@app.post("/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    system = _get_system()
    session = system.session_manager.create_session(
        objective_id=req.objective_id,
        title=req.title,
    )
    system.file_store.save_session(session.id, session.model_dump())
    return session.model_dump()


@app.get("/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    system = _get_system()
    sessions = []
    for sid in system.file_store.list_sessions():
        data = system.file_store.load_session(sid)
        if data:
            sessions.append(data)
    return sessions


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    system = _get_system()
    session = system.session_manager._sessions.get(session_id)
    if session is None:
        data = system.file_store.load_session(session_id)
        if not data:
            raise HTTPException(status_code=404, detail="Session not found")
        return data
    return session.model_dump()


# ───────────────────────────────
# 对话
# ───────────────────────────────

@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest) -> Any:
    system = _get_system()
    session = system.session_manager._sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if req.stream:
        return StreamingResponse(
            _stream_chat_chunks(system, session_id, req.message),
            media_type="text/event-stream",
        )
    else:
        # 非流式：累积完整回复
        content_parts = []
        try:
            async for chunk in system.agent_loop.run(session, req.message):
                content_parts.append(chunk.content)
        except Exception as e:
            logger.exception(f"[Web] Chat error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # 每次对话结束后自动持久化会话内容
            try:
                system.file_store.save_session(session_id, session.model_dump())
            except Exception:
                logger.exception(f"[Web] Failed to save session {session_id}")
        return {
            "session_id": session_id,
            "content": "".join(content_parts),
        }


# ───────────────────────────────
# 会话 Fork
# ───────────────────────────────

@app.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, req: ForkRequest) -> dict[str, Any]:
    system = _get_system()
    session = system.session_manager._sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    fork = system.session_manager.fork_at(
        session_id,
        req.entry_id or session.current_leaf_id,
        fork_content="User initiated fork via web API",
    )
    if not fork:
        raise HTTPException(status_code=400, detail="Fork failed")
    return fork.model_dump()


# ───────────────────────────────
# 会话更新与删除
# ───────────────────────────────

@app.put("/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> dict[str, Any]:
    system = _get_system()
    session = system.session_manager._sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if req.title is not None:
        session.title = req.title
    system.file_store.save_session(session_id, session.model_dump())
    return session.model_dump()


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    system = _get_system()
    if session_id not in system.session_manager._sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    # 1. 清理运行时状态（AgentLoopSession 及其锁、失败追踪器等）
    system.agent_loop.clear_session_runtime(session_id)

    # 2. 清理持久化数据
    del system.session_manager._sessions[session_id]
    system.file_store.delete(f"sessions/{session_id}.json")
    await system._save_state()

    return {"status": "deleted", "session_id": session_id}


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
    candidate = system.memory_manager._l1_working.pop(node_id, None)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Knowledge candidate not found")
    system.memory_manager.promote_to_l2(candidate, auto_confirm=True)
    await system.event_bus.publish(
        Event(
            type="knowledge.confirmed",
            payload={"node_id": node_id},
            source="web",
        )
    )
    return {"status": "confirmed", "node_id": node_id}


# ───────────────────────────────
# 状态保存
# ───────────────────────────────

@app.post("/save")
async def save_state() -> dict[str, str]:
    system = _get_system()
    await system._save_state()
    return {"status": "saved"}


# ───────────────────────────────
# 可观测性（Observability）
# ───────────────────────────────

@app.get("/observability/errors")
async def get_errors(limit: int = 500) -> dict[str, Any]:
    """
    返回全链路错误日志，聚合多个来源：
    1. errors.jsonl —— 系统检测到的错误/警告事件
    2. trace_errors —— Trace span 中的 error 字段
    3. audit_errors —— audit.jsonl 中的 decision=error 记录
    """
    system = _get_system()
    data_dir = Path(system.observability.data_dir)
    errors: list[dict[str, Any]] = []

    # 1. errors.jsonl
    errors_path = data_dir / "errors.jsonl"
    if errors_path.exists():
        with open(errors_path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rec["source_type"] = "system_event"
                    errors.append(rec)
                except Exception:
                    pass

    # 2. trace span errors
    for f in data_dir.glob("trace_*.json"):
        try:
            data = json.loads(f.read_text())
            for span in data.get("spans", []):
                if span.get("error"):
                    errors.append({
                        "timestamp": span.get("start_time") or data.get("timestamp"),
                        "level": "error",
                        "category": "trace",
                        "type": "span.error",
                        "message": span["error"],
                        "trace_id": data.get("trace_id"),
                        "session_id": data.get("session_id"),
                        "source": "trace",
                        "source_type": "trace_span",
                        "details": {
                            "span_id": span.get("span_id"),
                            "span_name": span.get("name"),
                            "duration_ms": span.get("duration_ms"),
                            "tags": span.get("tags"),
                        },
                    })
        except Exception:
            pass

    # 3. audit errors
    audit_path = data_dir / "audit.jsonl"
    if audit_path.exists():
        with open(audit_path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("decision") == "error" or rec.get("success") is False:
                        errors.append({
                            "timestamp": rec.get("timestamp"),
                            "level": "error",
                            "category": "audit",
                            "type": rec.get("event_type", "audit.error"),
                            "message": rec.get("reason", "Audit error"),
                            "trace_id": rec.get("trace_id"),
                            "session_id": rec.get("session_id"),
                            "source": rec.get("source", "audit"),
                            "source_type": "audit_log",
                            "details": rec,
                        })
                except Exception:
                    pass

    # 按时间排序，最新的在前
    errors.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"errors": errors[:limit], "total": len(errors)}

@app.get("/observability/flows/{session_id}")
async def get_flow(session_id: str) -> dict[str, Any]:
    system = _get_system()
    flow_path = Path(system.observability.data_dir) / f"flow_{session_id}.json"
    if not flow_path.exists():
        raise HTTPException(status_code=404, detail="Flow not found")
    return json.loads(flow_path.read_text())

@app.get("/observability/traces")
async def list_traces(limit: int = 100) -> list[dict[str, Any]]:
    system = _get_system()
    trace_dir = Path(system.observability.data_dir)
    traces = []
    for f in sorted(trace_dir.glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            traces.append({
                "trace_id": data.get("trace_id"),
                "session_id": data.get("session_id"),
                "timestamp": data.get("timestamp"),
                "duration_ms": data.get("duration_ms"),
                "span_count": len(data.get("spans", [])),
            })
        except Exception:
            pass
    return traces[:limit]


@app.get("/observability/traces/{trace_id}")
async def get_trace(trace_id: str) -> dict[str, Any]:
    system = _get_system()
    trace_path = Path(system.observability.data_dir) / f"trace_{trace_id}.json"
    if not trace_path.exists():
        raise HTTPException(status_code=404, detail="Trace not found")
    return json.loads(trace_path.read_text())


@app.get("/observability/events")
async def list_events(limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    system = _get_system()
    events_path = Path(system.observability.data_dir) / "events.jsonl"
    if not events_path.exists():
        return []
    lines = events_path.read_text().strip().split("\n")
    selected = lines[-(offset + limit):-offset if offset else None]
    result = []
    for line in selected:
        line = line.strip()
        if line:
            try:
                result.append(json.loads(line))
            except Exception:
                pass
    return result


@app.get("/observability/metrics")
async def get_metrics() -> dict[str, Any]:
    system = _get_system()
    return system.observability.get_metrics_summary()


@app.get("/observability/runtimes")
async def get_runtimes() -> dict[str, Any]:
    """返回当前活跃的 Session 运行时状态（含 trace/span 观测）。"""
    system = _get_system()
    agent_loop = system.agent_loop
    obs = system.observability

    runtimes = []
    for sid, runtime in agent_loop._session_runtimes.items():
        trace = obs.get_trace_by_session(sid) if obs else None
        spans = obs.get_spans_by_session(sid) if trace else []

        runtimes.append({
            "session_id": sid,
            "state": runtime.state.value,
            "chat_only_mode": runtime._chat_only_mode,
            "chat_only_success_turns": runtime._chat_only_success_turns,
            "failure_tracker": {
                "tracked_tools": list(runtime._failure_tracker._counts.keys()),
                "banned_tools": [
                    tool_id for tool_id in runtime._failure_tracker._counts.keys()
                    if runtime._failure_tracker.is_banned(tool_id, 0)
                ],
            },
            "lock_acquired": runtime.lock.locked() if hasattr(runtime, "lock") else False,
            "last_accessed": agent_loop._session_last_accessed.get(sid),
            "trace": {
                "trace_id": trace.trace_id if trace else None,
                "span_count": len(trace.spans) if trace else 0,
                "active_spans": [s.name for s in spans],
                "duration_ms": trace.duration_ms if trace else None,
            } if trace else None,
        })

    return {
        "active_runtime_count": len(agent_loop._session_runtimes),
        "total_session_count": len(system.session_manager._sessions),
        "runtimes": runtimes,
    }


@app.post("/sessions/{session_id}/reset-runtime")
async def reset_session_runtime(session_id: str) -> dict[str, str]:
    """重置指定 session 的运行时状态（不清除聊天记录）。"""
    system = _get_system()
    if session_id not in system.session_manager._sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    system.agent_loop.clear_session_runtime(session_id)
    return {"status": "runtime_reset", "session_id": session_id}


@app.get("/observability/logs")
async def get_logs(limit: int = 500) -> dict[str, Any]:
    """
    返回两类日志：
    1. trace_errors: 所有 trace 中包含 error 的 span
    2. event_logs: events.jsonl 中的记录（目前均为 info，预留扩展）
    """
    system = _get_system()
    trace_dir = Path(system.observability.data_dir)
    errors = []
    for f in trace_dir.glob("trace_*.json"):
        try:
            data = json.loads(f.read_text())
            for span in data.get("spans", []):
                if span.get("error"):
                    errors.append({
                        "trace_id": data.get("trace_id"),
                        "session_id": data.get("session_id"),
                        "span_id": span.get("span_id"),
                        "span_name": span.get("name"),
                        "error": span.get("error"),
                        "timestamp": data.get("timestamp"),
                    })
        except Exception:
            pass

    events_path = Path(system.observability.data_dir) / "events.jsonl"
    event_logs = []
    if events_path.exists():
        with open(events_path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    event_logs.append(rec)
                except Exception:
                    pass

    return {
        "trace_errors": errors[-limit:],
        "event_logs": event_logs[-limit:],
    }


@app.get("/observability/events/stream")
async def events_stream() -> StreamingResponse:
    """SSE 实时推送新事件。"""
    system = _get_system()

    async def generator() -> AsyncGenerator[str, None]:
        last_size = 0
        events_path = Path(system.observability.data_dir) / "events.jsonl"
        while True:
            if events_path.exists():
                current_size = events_path.stat().st_size
                if current_size > last_size:
                    with open(events_path, "r", encoding="utf-8") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                    last_size = current_size
            await asyncio.sleep(1)

    return StreamingResponse(generator(), media_type="text/event-stream")
