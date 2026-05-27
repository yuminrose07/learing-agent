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

from dotenv import load_dotenv

from learning_agent.ai import ProviderConfig


class Config:
    """
    系统配置，支持环境变量 + 配置文件混合读取。
    """

    def __init__(self, config_path: Optional[str] = None, dotenv_path: Optional[str] = None):
        # 0. 加载 .env 文件（不会覆盖已存在的环境变量）
        if dotenv_path:
            load_dotenv(dotenv_path=dotenv_path, override=False)
        else:
            # 自动查找当前目录或上级目录的 .env
            load_dotenv(override=False)

        # 1. 加载配置文件（低优先级）
        file_values = self._load_config_file(config_path)

        # 2. 环境变量覆盖（高优先级）
        self.data_dir = self._env_or_file(
            "LA_DATA_DIR", file_values, "data_dir", ".learning_agent_data"
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
            base_url=os.getenv("OPENAI_BASE_URL") or provider_from_file.get("base_url", "https://api.moonshot.cn/v1"),
            model=os.getenv("LA_MODEL") or provider_from_file.get("model", "kimi-2.6"),
            timeout=float(
                os.getenv("LA_TIMEOUT") or provider_from_file.get("timeout", "60.0")
            ),
            max_retries=int(
                os.getenv("LA_MAX_RETRIES") or provider_from_file.get("max_retries", "3")
            ),
        )

        # 可选：支持多 Provider 切换（预留）
        self.provider_type = provider_from_file.get("type", "openai")

        # 概念抽取（学习卷 absorbing 阶段尾任务）。
        # ``concept_extractor_model`` 若为 None，沿用主 provider.model。
        # 想接 Haiku 4.5 / 其它 cheap 模型时，把字段值填成路由库认识的标识
        # （依赖 base_url 后面的 Python 路由库做跨 provider 转发）。
        learning_unit_from_file = file_values.get("learning_unit", {})
        self.concept_extractor_model: Optional[str] = (
            os.getenv("LA_CONCEPT_MODEL")
            or learning_unit_from_file.get("concept_extractor_model")
        )
        self.concept_extraction_threshold: float = float(
            os.getenv("LA_CONCEPT_THRESHOLD")
            or learning_unit_from_file.get("concept_extraction_threshold", "0.6")
        )

        # outputting 阶段 TEACH 链路（B5 E4）。两者皆可为 None，回退到
        # provider.default_model。建议在 config.yaml 里指向同一颗 cheap-tier
        # 模型，例如 ``claude-haiku-4-5-20251001``。
        self.teach_generator_model: Optional[str] = (
            os.getenv("LA_TEACH_GENERATOR_MODEL")
            or learning_unit_from_file.get("teach_generator_model")
        )
        self.teach_judge_model: Optional[str] = (
            os.getenv("LA_TEACH_JUDGE_MODEL")
            or learning_unit_from_file.get("teach_judge_model")
        )

        # 闲聊陪伴意图分类的 LLM 兜底（关键字未命中时调一次）。建议指向
        # cheap-tier，例如本项目接入 DashScope 时用 ``qwen-turbo``；
        # 主模型 ``qwen-max`` 的延迟和单价对"6 选 1"分类都过重。
        self.companion_intent_model: Optional[str] = (
            os.getenv("LA_COMPANION_INTENT_MODEL")
            or learning_unit_from_file.get("companion_intent_model")
        )

        # Tool Guard 配置
        tool_guard_from_file = file_values.get("tool_guard", {})
        rules_from_file = tool_guard_from_file.get("rules", {})
        self.tool_guard = {
            "bash_mode": os.getenv("LA_BASH_MODE") or tool_guard_from_file.get("bash_mode", "disabled"),
            "bash_allowlist": tool_guard_from_file.get("bash_allowlist", []),
            "bash_denylist": tool_guard_from_file.get("bash_denylist", []),
            "rules": {
                "allow": rules_from_file.get("allow", []),
                "deny": rules_from_file.get("deny", []),
                "ask": rules_from_file.get("ask", []),
            },
        }

        web_search_from_file = file_values.get("web_search", {})
        allowed_source_types = os.getenv("LA_WEB_SEARCH_ALLOWED_SOURCE_TYPES")
        if allowed_source_types:
            parsed_allowed_source_types = [
                item.strip() for item in allowed_source_types.split(",") if item.strip()
            ]
        else:
            parsed_allowed_source_types = web_search_from_file.get(
                "allowed_source_types",
                [
                    "official_docs",
                    "official_blog",
                    "github_repo",
                    "github_issue",
                    "community_forum",
                    "blog",
                    "paper",
                    "news",
                    "aggregator",
                ],
            )
        self.web_search = {
            "enabled": (
                os.getenv("LA_WEB_SEARCH_ENABLED")
                or str(web_search_from_file.get("enabled", True))
            ).lower() == "true",
            "provider": os.getenv("LA_WEB_SEARCH_PROVIDER")
            or web_search_from_file.get("provider", "builtin"),
            "default_top_k": int(
                os.getenv("LA_WEB_SEARCH_DEFAULT_TOP_K")
                or web_search_from_file.get("default_top_k", "5")
            ),
            "max_top_k": int(
                os.getenv("LA_WEB_SEARCH_MAX_TOP_K")
                or web_search_from_file.get("max_top_k", "10")
            ),
            "default_limit_chars": int(
                os.getenv("LA_WEB_FETCH_DEFAULT_LIMIT_CHARS")
                or web_search_from_file.get("default_limit_chars", "12000")
            ),
            "timeout_seconds": float(
                os.getenv("LA_WEB_SEARCH_TIMEOUT_SECONDS")
                or web_search_from_file.get("timeout_seconds", "12")
            ),
            "tls_ca_bundle_path": os.getenv("LA_WEB_SEARCH_CA_BUNDLE_PATH")
            or web_search_from_file.get("tls_ca_bundle_path"),
            "prefer_system_trust_store": (
                os.getenv("LA_WEB_SEARCH_PREFER_SYSTEM_TRUST_STORE")
                or str(web_search_from_file.get("prefer_system_trust_store", True))
            ).lower() == "true",
            "allowed_source_types": parsed_allowed_source_types,
        }

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
            "tool_guard": self.tool_guard,
            "web_search": self.web_search,
        }
