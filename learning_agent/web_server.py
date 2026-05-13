import sys

from learning_agent.web import web_server as _impl

sys.modules[__name__] = _impl
