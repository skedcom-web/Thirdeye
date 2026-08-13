"""Shared FastAPI dependencies for the workbench and Phase 3 operations routes.

Split out from app.py so operations_routes.py can depend on these directly
instead of reaching back into app.py (which would be a real circular
import, not just an ordering inconvenience, since app.py imports
operations_routes at the bottom of create_app).

Dependencies must live at module scope: with `from __future__ import
annotations` every annotation is a string, and FastAPI resolves those
against module globals. A closure-local alias silently degrades into a
query parameter instead of a dependency -- this bit us once already in
Phase 1 (see git history), which is why this file exists.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Callable, Iterator

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..db import connect
from ..fetching import Fetcher, HttpFetcher
from ..operations import auth

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SESSION_COOKIE = "thirdeye_session"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["pct"] = lambda value: f"{float(value) * 100:.1f}%"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_fetcher() -> Iterator[HttpFetcher]:
    """A real fetcher by default -- overridden in tests via
    app.dependency_overrides so certification tests never touch the network."""
    fetcher = HttpFetcher()
    try:
        yield fetcher
    finally:
        fetcher.close()


def get_fetcher_factory() -> Callable[[], Fetcher]:
    """A factory, not an instance -- for background jobs (Module 6), which
    run in a thread that outlives the request and cannot share a
    request-scoped fetcher. Production gets the real HttpFetcher class
    (called fresh per use); tests override this to return a factory that
    hands back a pre-configured OfflineFetcher instead."""
    return HttpFetcher


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
FetcherDep = Annotated[HttpFetcher, Depends(get_fetcher)]
FetcherFactory = Annotated[Callable[[], Fetcher], Depends(get_fetcher_factory)]
Config = Annotated[Settings, Depends(get_settings)]


class RedirectToLogin(HTTPException):
    """Raised by `require_login`. 303 forces the browser to re-GET /login
    regardless of the original method; the JSON body FastAPI attaches is
    irrelevant since browsers act on the Location header for 3xx anyway."""

    def __init__(self, next_path: str) -> None:
        super().__init__(status_code=303, headers={"Location": f"/login?next={next_path}"})


class RedirectToSetup(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=303, headers={"Location": "/setup"})


def get_current_user(request: Request, conn: Conn) -> auth.User | None:
    return auth.get_session_user(conn, request.cookies.get(SESSION_COOKIE))


CurrentUser = Annotated[auth.User | None, Depends(get_current_user)]


def require_login(request: Request, conn: Conn, current_user: CurrentUser) -> auth.User:
    if current_user is None:
        if not auth.has_any_users(conn):
            raise RedirectToSetup()
        raise RedirectToLogin(next_path=request.url.path)
    return current_user


LoggedIn = Annotated[auth.User, Depends(require_login)]


def require_permission(permission: str):
    def check(current_user: LoggedIn) -> auth.User:
        if not current_user.has_permission(permission):
            raise HTTPException(
                status_code=403,
                detail=f"your role ({current_user.role}) lacks permission: {permission}",
            )
        return current_user

    return check


RequireReview = Annotated[auth.User, Depends(require_permission(auth.PERM_REVIEW_RECORDS))]
RequireEscalate = Annotated[auth.User, Depends(require_permission(auth.PERM_ESCALATE_RECORDS))]
RequireCertify = Annotated[auth.User, Depends(require_permission(auth.PERM_RUN_CERTIFICATION))]
RequireSources = Annotated[auth.User, Depends(require_permission(auth.PERM_MANAGE_SOURCES))]
RequireDistricts = Annotated[auth.User, Depends(require_permission(auth.PERM_MANAGE_DISTRICTS))]
RequireStates = Annotated[auth.User, Depends(require_permission(auth.PERM_MANAGE_STATES))]
RequireDepartments = Annotated[auth.User, Depends(require_permission(auth.PERM_MANAGE_DEPARTMENTS))]
RequirePublish = Annotated[auth.User, Depends(require_permission(auth.PERM_PUBLISH))]
RequireUsers = Annotated[auth.User, Depends(require_permission(auth.PERM_MANAGE_USERS))]
