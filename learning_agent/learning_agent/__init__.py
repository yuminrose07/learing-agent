from importlib import import_module

__all__ = [
    "Config",
    "Extension",
    "ExtensionContext",
    "ExtensionManager",
    "LearningAgentSystem",
    "SessionManager",
    "create_builtin_extensions",
    "interactive_cli",
]


def __getattr__(name: str):
    module_map = {
        "Config": "learning_agent.learning_agent.config",
        "Extension": "learning_agent.learning_agent.extension_manager",
        "ExtensionContext": "learning_agent.learning_agent.extension_manager",
        "ExtensionManager": "learning_agent.learning_agent.extension_manager",
        "LearningAgentSystem": "learning_agent.learning_agent.main",
        "SessionManager": "learning_agent.learning_agent.session_manager",
        "create_builtin_extensions": "learning_agent.learning_agent.extensions",
        "interactive_cli": "learning_agent.learning_agent.main",
    }
    module_name = module_map.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name), name)
