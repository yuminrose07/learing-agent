"""
Learning-Agent 全局配置。

支持三种配置来源（优先级从高到低）：
1. 环境变量
2. 配置文件（YAML / JSON / TOML）
3. 代码默认值

配置文件默认查找路径（按顺序）：
- $LA_CONFIG_PATH（环境变量指定）
- ./config.yaml
- ./config.yml
- ./config.json
- ~/.learning_agent/config.yaml
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Optional

from learning_agent.models import ProviderConfig


class Config:
    """
    系统配置，支持环境变量 + 配置文件混合读取。
    """

    def __init__(self, config_path: Optional[str] = None):
        # 1. 加载配置文件（低优先级）
        file_values = self._load_config_file(config_path)

        # 2. 环境变量覆盖（高优先级）
        self.data_dir = self._env_or_file(
            "LA_DATA_DIR", file_values, "data_dir", ".learning_agent_data"
        )
        self.observability_dir = self._env_or_file(
            "LA_OBS_DIR", file_values, "observability_dir", ".observability"
        )
        self.auto_confirm_knowledge = self._env_or_file(
            "LA_AUTO_CONFIRM", file_values, "auto_confirm_knowledge", "false"
        ).lower() == "true"
        self.log_level = self._env_or_file(
            "LA_LOG_LEVEL", file_values, "log_level", "INFO"
        )

        # Provider 配置（支持嵌套展开）
        provider_from_file = file_values.get("provider", {})
        self.provider_config = ProviderConfig(
            api_key=os.getenv("OPENAI_API_KEY") or provider_from_file.get("api_key"),
            base_url=os.getenv("OPENAI_BASE_URL") or provider_from_file.get("base_url"),
            model=os.getenv("LA_MODEL") or provider_from_file.get("model", "gpt-4o"),
            timeout=float(
                os.getenv("LA_TIMEOUT") or provider_from_file.get("timeout", "60.0")
            ),
            max_retries=int(
                os.getenv("LA_MAX_RETRIES") or provider_from_file.get("max_retries", "3")
            ),
        )

        # 可选：支持多 Provider 切换（预留）
        self.provider_type = provider_from_file.get("type", "openai")

    def _load_config_file(self, config_path: Optional[str]) -> dict[str, Any]:
        """尝试加载配置文件，返回解析后的字典。"""
        paths = []
        if config_path:
            paths.append(pathlib.Path(config_path))
        else:
            paths.extend([
                pathlib.Path("config.yaml"),
                pathlib.Path("config.yml"),
                pathlib.Path("config.json"),
                pathlib.Path.home() / ".learning_agent" / "config.yaml",
            ])

        for p in paths:
            if p.exists():
                return self._parse_file(p)
        return {}

    def _parse_file(self, path: pathlib.Path) -> dict[str, Any]:
        """根据后缀解析 YAML / JSON / TOML。"""
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()

        if suffix in (".yaml", ".yml"):
            try:
                import yaml
                return yaml.safe_load(text) or {}
            except ImportError:
                raise RuntimeError(
                    f"Config file '{path}' requires PyYAML. "
                    f"Install it: pip install pyyaml"
                )
        elif suffix == ".json":
            return json.loads(text)
        elif suffix == ".toml":
            try:
                import tomllib
                return tomllib.loads(text)
            except ImportError:
                try:
                    import tomli
                    return tomli.loads(text)
                except ImportError:
                    raise RuntimeError(
                        f"Config file '{path}' requires tomllib/tomli. "
                        f"Install it: pip install tomli"
                    )
        else:
            # 默认尝试 JSON，失败再试 YAML
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                try:
                    import yaml
                    return yaml.safe_load(text) or {}
                except ImportError:
                    raise RuntimeError(
                        f"Cannot parse config file '{path}'. "
                        f"Supported formats: .yaml, .yml, .json, .toml"
                    )

    @staticmethod
    def _env_or_file(
        env_name: str,
        file_values: dict[str, Any],
        file_key: str,
        default: Any,
    ) -> str:
        """优先级：环境变量 > 配置文件 > 默认值。"""
        env_val = os.getenv(env_name)
        if env_val is not None:
            return env_val
        file_val = file_values.get(file_key)
        if file_val is not None:
            return str(file_val)
        return str(default)

    def validate(self) -> list[str]:
        """验证配置，返回错误列表。"""
        errors = []
        if not self.provider_config.api_key:
            errors.append(
                "API key is missing. Set OPENAI_API_KEY environment variable "
                "or add 'provider.api_key' to your config file."
            )
        if not self.provider_config.model:
            errors.append("Model is not configured.")
        return errors

    def to_dict(self) -> dict[str, Any]:
        """导出当前配置为字典（敏感信息已脱敏）。"""
        return {
            "data_dir": self.data_dir,
            "observability_dir": self.observability_dir,
            "provider": {
                "type": self.provider_type,
                "model": self.provider_config.model,
                "base_url": self.provider_config.base_url,
                "timeout": self.provider_config.timeout,
                "max_retries": self.provider_config.max_retries,
                "api_key": "***" if self.provider_config.api_key else None,
            },
            "auto_confirm_knowledge": self.auto_confirm_knowledge,
            "log_level": self.log_level,
        }
