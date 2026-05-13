"""
文件持久化层：所有用户可见的数据以人类可读的文本文件持久化。
- 会话：JSONL / JSON
- 知识节点：Markdown（内容）+ JSON（元数据）
- 配置：JSON
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FileStore:
    """
    本地文件存储管理器。
    目录结构：
    base_dir/
      sessions/
        {session_id}.json
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
        for sub in ["sessions", "memory", "materials", "objectives"]:
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

    # ─── 领域方法 ───

    def save_session(self, session_id: str, data: dict[str, Any]) -> None:
        self.write_json(f"sessions/{session_id}.json", data)

    def load_session(self, session_id: str) -> Optional[dict[str, Any]]:
        return self.read_json(f"sessions/{session_id}.json")

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
        return [f.stem for f in path.glob("*.json")]

    def list_objectives(self) -> list[str]:
        path = self.base_dir / "objectives"
        if not path.exists():
            return []
        return [f.stem for f in path.glob("*.json")]

    def get_base_dir(self) -> str:
        return str(self.base_dir)
