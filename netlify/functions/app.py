"""Netlify Function entry point: adapts the FastAPI ASGI app to the
Lambda-style event/response shape Netlify's Python function runtime invokes.

THIRDEYE_DATA_DIR must point at /tmp before goengine.workbench.app is
imported -- importing it runs create_app() at module load time (see
goengine/workbench/app.py's `app = create_app()`), which calls
Settings.load().ensure_dirs() immediately. Settings.load() defaults that
directory to the project root when the env var is unset, and Netlify's
Lambda filesystem is read-only outside /tmp, so an unset var here crashes
every cold start. This only matters for that startup mkdir and the local
read-through PDF cache in repository.py -- actual data (Turso) is
unaffected and durable regardless of what happens to /tmp between
invocations.
"""

import os

os.environ.setdefault("THIRDEYE_DATA_DIR", "/tmp/thirdeye")

from mangum import Mangum

from goengine.workbench.app import app

handler = Mangum(app, lifespan="off")
