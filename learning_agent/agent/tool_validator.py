"""
工具输入校验器：在工具执行前完成结构合规校验。

校验顺序：
1. 工具名存在性
2. JSON 可解析性（arguments 必须是 dict）
3. Pydantic 强校验（若 ToolDefinition.input_model 存在）
4. JSON Schema 校验（fallback）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, ValidationError as PydanticValidationError

from learning_agent.ai import ToolCall

logger = logging.getLogger(__name__)


class ValidationErrorDetail(BaseModel):
    param: str
    issue: str


class ToolInputValidator:
    """工具输入参数校验器，不侵入业务逻辑。"""

    def __init__(self, tool_definition_provider: Any):
        """
        初始化工具输入校验器。

        Args:
            tool_definition_provider: 提供工具定义的对象，必须实现
                get_tool_definition(tool_id) -> Optional[ToolDefinition] 方法
        """
        self._registry = tool_definition_provider
        self._jsonschema_validators: dict[str, Any] = {}

    async def validate(self, tool_call: ToolCall) -> Optional[list[ValidationErrorDetail]]:
        """
        校验工具调用参数。
        返回 None 表示通过；返回列表表示失败详情。
        """
        # 1. 工具名存在性
        tool_def = self._registry.get_tool_definition(tool_call.tool_id)
        if tool_def is None:
            return [ValidationErrorDetail(param="tool_id", issue=f"No such tool available: {tool_call.tool_id}")]

        # 2. JSON 可解析性（arguments 必须是 dict，流式拼接后已由 AgentLoop 保证）
        if not isinstance(tool_call.arguments, dict):
            return [ValidationErrorDetail(param="arguments", issue=f"Expected dict, got {type(tool_call.arguments).__name__}")]

        # 3. Pydantic 强校验（优先）
        if tool_def.input_model is not None:
            return self._validate_pydantic(tool_call, tool_def.input_model)

        # 4. JSON Schema 校验（fallback）
        if tool_def.parameters:
            return self._validate_jsonschema(tool_call, tool_def.parameters)

        # 无任何 schema，直接通过
        return None

    def _validate_pydantic(
        self,
        tool_call: ToolCall,
        input_model: type[BaseModel],
    ) -> Optional[list[ValidationErrorDetail]]:
        """使用 Pydantic model_validate 进行强校验。"""
        try:
            input_model.model_validate(tool_call.arguments)
            return None
        except PydanticValidationError as e:
            errors: list[ValidationErrorDetail] = []
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"])
                errors.append(ValidationErrorDetail(param=loc, issue=err["msg"]))
            return errors
        except Exception as e:
            logger.warning(f"[ToolInputValidator] Pydantic validation unexpected error for '{tool_call.tool_id}': {e}")
            return [ValidationErrorDetail(param="_root", issue=str(e))]

    def _validate_jsonschema(
        self,
        tool_call: ToolCall,
        schema: dict[str, Any],
    ) -> Optional[list[ValidationErrorDetail]]:
        """使用 jsonschema 进行校验。"""
        try:
            import jsonschema
            from jsonschema import exceptions as jsonschema_exceptions
        except ImportError:
            logger.warning("[ToolInputValidator] jsonschema not installed, skipping JSON Schema validation.")
            return None

        try:
            validator = self._get_jsonschema_validator(tool_call.tool_id, schema)
            validator.validate(tool_call.arguments)
            return None
        except jsonschema_exceptions.ValidationError as e:
            errors: list[ValidationErrorDetail] = []
            # 收集所有验证错误
            validator = self._get_jsonschema_validator(tool_call.tool_id, schema)
            for err in validator.iter_errors(tool_call.arguments):
                loc = ".".join(str(x) for x in err.path) if err.path else "_root"
                errors.append(ValidationErrorDetail(param=loc, issue=err.message))
            return errors or [ValidationErrorDetail(param="_root", issue=str(e))]
        except Exception as e:
            logger.warning(f"[ToolInputValidator] JSON Schema validation unexpected error for '{tool_call.tool_id}': {e}")
            # 降级：仅检查 type: object + 必填字段存在性
            return self._loose_validate_jsonschema(tool_call, schema)

    def _get_jsonschema_validator(self, tool_id: str, schema: dict[str, Any]) -> Any:
        """获取或编译缓存的 jsonschema 校验器。"""
        if tool_id not in self._jsonschema_validators:
            try:
                import jsonschema
                from jsonschema import validators as jsonschema_validators
                # 尝试 Draft 2020-12，fallback 到 Draft 7
                draft_cls = getattr(jsonschema_validators, "Draft202012Validator", None)
                if draft_cls is None:
                    draft_cls = getattr(jsonschema_validators, "Draft7Validator", None)
                if draft_cls is not None:
                    checker = draft_cls.TYPE_CHECKER
                    validator_cls = jsonschema_validators.extend(draft_cls, type_checker=checker)
                    self._jsonschema_validators[tool_id] = validator_cls(schema)
                else:
                    self._jsonschema_validators[tool_id] = jsonschema.Draft7Validator(schema)
            except Exception as e:
                logger.warning(f"[ToolInputValidator] Failed to compile JSON Schema for '{tool_id}': {e}")
                # 返回一个最小校验器，下次会触发降级
                self._jsonschema_validators[tool_id] = None
        return self._jsonschema_validators[tool_id]

    def _loose_validate_jsonschema(
        self,
        tool_call: ToolCall,
        schema: dict[str, Any],
    ) -> Optional[list[ValidationErrorDetail]]:
        """降级校验：仅检查必填字段存在性。"""
        errors: list[ValidationErrorDetail] = []
        required = schema.get("required", [])
        for field in required:
            if field not in tool_call.arguments:
                errors.append(ValidationErrorDetail(param=field, issue="required field missing"))
        return errors if errors else None

    @staticmethod
    def format_validation_error(
        tool_id: str,
        call_id: Optional[str],
        errors: list[ValidationErrorDetail],
    ) -> str:
        """构造标准校验错误信息文本（回流给 LLM）。"""
        lines = [
            "[Tool Input Validation Failed]",
            f"Tool: {tool_id}",
            f"Call ID: {call_id or 'N/A'}",
            "",
            "Errors:",
        ]
        for err in errors:
            lines.append(f"- Parameter '{err.param}': {err.issue}")
        lines.append("")
        lines.append("Hint: Please fix the parameters and retry.")
        return "\n".join(lines)
