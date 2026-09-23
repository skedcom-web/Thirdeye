"""FastAPI Cloud entry point.

`fastapi run` (invoked by FastAPI Cloud's container at startup) looks for a
default file like main.py or app.py at the project root -- the real app
lives nested at goengine/workbench/app.py, so this just re-exports it.
"""

from goengine.workbench.app import app

__all__ = ["app"]
