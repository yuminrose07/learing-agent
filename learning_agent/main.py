from learning_agent.learning_agent.main import *

if __name__ == "__main__":
    import argparse
    import asyncio
    import os
    import sys
    import uvicorn

    _parser = argparse.ArgumentParser(description="Learning-Agent CLI")
    _parser.add_argument("--config", "-c", type=str, default=None)
    _parser.add_argument("--show-config", action="store_true")
    _parser.add_argument("--web", action="store_true")
    _parser.add_argument("--host", type=str, default="127.0.0.1")
    _parser.add_argument("--port", type=int, default=8000)
    _args = _parser.parse_args()

    if _args.web:
        if _args.config:
            os.environ["LA_CONFIG_PATH"] = _args.config
        uvicorn.run("learning_agent.web.web_server:app", host=_args.host, port=_args.port, reload=False)
    else:
        asyncio.run(interactive_cli(sys.argv[1:]))
