from importlib import import_module

__all__ = [
    "AgentLoop",
    "AgentLoopSession",
    "AgentState",
    "EventBus",
    "HookSystem",
    "MetricsStore",
    "ObservabilityCollector",
    "ToolFailureTracker",
    "ToolRegistry",
    "ToolInputValidator",
]


def __getattr__(name: str):
    module_map = {
        "AgentLoop": "learning_agent.agent.agent_loop",
        "AgentLoopSession": "learning_agent.agent.agent_loop",
        "AgentState": "learning_agent.agent.agent_loop",
        "EventBus": "learning_agent.agent.event_bus",
        "HookSystem": "learning_agent.agent.hook_system",
        "MetricsStore": "learning_agent.agent.observability",
        "ObservabilityCollector": "learning_agent.agent.observability",
        "ToolFailureTracker": "learning_agent.agent.tool_failure_tracker",
        "ToolRegistry": "learning_agent.agent.tool_registry",
        "ToolInputValidator": "learning_agent.agent.tool_validator",
    }
    module_name = module_map.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name), name)
