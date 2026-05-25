"""
文件持久化层：所有用户可见的数据以人类可读的文本文件持久化。
- 会话：JSONL / JSON
- 知识节点：Markdown（内容）+ JSON（元数据）
- 配置：JSON
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FileStore:
    """
    本地文件存储管理器。
    目录结构：
    base_dir/
      sessions/
        {session_id}.events.jsonl
      memory/
        knowledge_graph.json
        l1_working.json
      materials/
        {material_id}.json
      objectives/
        {objective_id}.json
      config.json
    """

    def __init__(self, base_dir: str = ".learning_agent_data"):
        self.base_dir = Path(base_dir)
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for sub in [
            "sessions",
            "memory",
            "memory/session_state",
            "memory/compact",
            "materials",
            "objectives",
            "learning_units",
        ]:
            (self.base_dir / sub).mkdir(parents=True, exist_ok=True)

    # ─── 通用读写 ───

    def write_json(self, relative_path: str, data: Any) -> None:
        path = self.base_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def read_json(self, relative_path: str) -> Optional[Any]:
        path = self.base_dir / relative_path
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def append_jsonl(self, relative_path: str, record: dict[str, Any]) -> None:
        path = self.base_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def read_jsonl(self, relative_path: str) -> list[dict[str, Any]]:
        path = self.base_dir / relative_path
        if not path.exists():
            return []
        records = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def write_text(self, relative_path: str, content: str) -> None:
        path = self.base_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def read_text(self, relative_path: str) -> Optional[str]:
        path = self.base_dir / relative_path
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def exists(self, relative_path: str) -> bool:
        return (self.base_dir / relative_path).exists()

    def delete(self, relative_path: str) -> bool:
        path = self.base_dir / relative_path
        if path.exists():
            path.unlink()
            return True
        return False

    # ─── Session event log ───

    def append_session_event(self, session_id: str, event: dict[str, Any]) -> None:
        self.append_jsonl(f"sessions/{session_id}.events.jsonl", event)

    def read_session_events(
        self,
        session_id: str,
        after_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        records = self.read_jsonl(f"sessions/{session_id}.events.jsonl")
        if after_seq is None:
            return records
        return [record for record in records if int(record.get("seq", 0)) > after_seq]

    def delete_session_events(self, session_id: str) -> bool:
        return self.delete(f"sessions/{session_id}.events.jsonl")

    # ─── Legacy session snapshot/delta migration-only helpers ───

    def legacy_save_session(self, session_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"sessions/{session_id}.json", data)

    def legacy_load_session(self, session_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"sessions/{session_id}.json")

    def legacy_append_session_delta(self, session_id: str, delta: dict[str, Any]) -> None:
        self.append_jsonl(f"sessions/{session_id}.jsonl", delta)

    def legacy_read_session_deltas(self, session_id: str) -> list[dict[str, Any]]:
        return self.read_jsonl(f"sessions/{session_id}.jsonl")

    def save_session_memory_state(self, session_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"memory/session_state/{session_id}.json", data)

    def load_session_memory_state(self, session_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"memory/session_state/{session_id}.json")

    def save_compact_metadata(self, session_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"memory/compact/{session_id}.meta.json", data)

    def load_compact_metadata(self, session_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"memory/compact/{session_id}.meta.json")

    def save_compact_summary(self, session_id: str, content: str) -> str:
        relative_path = f"memory/compact/{session_id}.summary.txt"
        self.write_text(relative_path, content)
        return relative_path

    def load_compact_summary(self, session_id: str) -> Optional[str]:
        return self.read_text(f"memory/compact/{session_id}.summary.txt")

    def save_knowledge_graph(self, data: dict[str, Any]) -> None:
        self.write_json("memory/knowledge_graph.json", data)

    def load_knowledge_graph(self) -> Optional[dict[str, Any]]:
        return self.read_json("memory/knowledge_graph.json")

    def save_objective(self, objective_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"objectives/{objective_id}.json", data)

    def load_objective(self, objective_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"objectives/{objective_id}.json")

    def save_material(self, material_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"materials/{material_id}.json", data)

    def load_material(self, material_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"materials/{material_id}.json")

    def list_sessions(self) -> list[str]:
        path = self.base_dir / "sessions"
        if not path.exists():
            return []
        event_ids = {
            f.name.removesuffix(".events.jsonl")
            for f in path.glob("*.events.jsonl")
        }
        legacy_ids = {
            f.stem
            for f in path.glob("*.json")
            if not f.name.endswith(".events.json")
        }
        return sorted(event_ids | legacy_ids)

    def list_legacy_sessions(self) -> list[str]:
        path = self.base_dir / "sessions"
        if not path.exists():
            return []
        return sorted(f.stem for f in path.glob("*.json"))

    def delete_matching(self, relative_glob: str) -> int:
        count = 0
        for path in self.base_dir.glob(relative_glob):
            if path.is_file():
                path.unlink()
                count += 1
        return count

    def list_objectives(self) -> list[str]:
        path = self.base_dir / "objectives"
        if not path.exists():
            return []
        return [f.stem for f in path.glob("*.json")]

    def save_learning_unit(self, unit_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"learning_units/{unit_id}.json", data)

    def load_learning_unit(self, unit_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"learning_units/{unit_id}.json")

    def delete_learning_unit(self, unit_id: str) -> bool:
        return self.delete(f"learning_units/{unit_id}.json")

    def list_learning_units(self) -> list[str]:
        path = self.base_dir / "learning_units"
        if not path.exists():
            return []
        return [f.stem for f in path.glob("*.json")]

    def get_base_dir(self) -> str:
        return str(self.base_dir)
