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
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from learning_agent.ai import AgentMode
from learning_agent.learning_agent.config import Config
from learning_agent.learning_agent.main import LearningAgentSystem

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
    mode: AgentMode = AgentMode.CHAT


class ConfirmKnowledgeRequest(BaseModel):
    node_id: str


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = None


class UpdateModeRequest(BaseModel):
    mode: AgentMode


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
    _system.clear_all_runtimes()
    logger.info("[Web] System initialized. All session runtimes cleared.")

    yield

    # 关闭时清理所有运行时
    _system.clear_all_runtimes()
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
    mode: AgentMode = AgentMode.CHAT,
) -> AsyncGenerator[str, None]:
    """将产品层对话流转换为 SSE 格式。"""
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
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    except ValueError:
        yield f"data: {json.dumps({'error': f'Session {session_id} not found'}, ensure_ascii=False)}\n\n"
    except Exception as e:
        logger.exception(f"[Web] Chat stream error: {e}")
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    finally:
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
    data = session.model_dump(exclude={"entries"})
    data["messages"] = []
    return data


@app.get("/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    system = _get_system()
    return [session.model_dump(exclude={"entries"}) for session in system.list_sessions()]


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
        )
    else:
        try:
            content = await system.collect_session_chat(
                session_id,
                req.message,
                mode=req.mode,
            )
        except ValueError:
            raise HTTPException(status_code=404, detail="Session not found")
        except Exception as e:
            logger.exception(f"[Web] Chat error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        return {
            "session_id": session_id,
            "content": content,
        }


@app.put("/sessions/{session_id}/mode")
async def update_session_mode(session_id: str, req: UpdateModeRequest) -> dict[str, Any]:
    system = _get_system()
    try:
        session = system.update_session_mode(session_id, req.mode)
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session.id,
        "mode": session.mode.value,
        "ask_state": session.ask_state.status,
    }


@app.get("/sessions/{session_id}/mode")
async def get_session_mode(session_id: str) -> dict[str, Any]:
    system = _get_system()
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session.id,
        "mode": session.mode.value,
        "ask_state": session.ask_state.status,
    }


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
                "mode": data.get("mode"),
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
    return system.get_runtime_overview()


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
