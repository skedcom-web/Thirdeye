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
import uuid
from pathlib import Path
from typing import Annotated, Callable, Iterator

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .. import intelligence
from ..config import Settings
from ..db import connect
from ..fetching import Fetcher, HttpFetcher
from ..operations import agent_auth, auth, citizen

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "thirdeye_session"
# Phase 4A -- citizens are a fully separate identity system (see
# schema_citizen.sql), so this is a different cookie name from SESSION_COOKIE
# above, not a shared one with a role flag. A citizen session simply doesn't
# exist as far as any staff-only LoggedIn route is concerned, and vice versa
# -- no route-by-route sweep needed to keep the two audiences apart.
CITIZEN_SESSION_COOKIE = "thirdeye_citizen_session"

# Third Eye 4.1.1 -- Citizen Experience Validation Edition. An anonymous,
# first-party, no-PII visitor identifier for engagement analytics (see
# operations/engagement.py) -- a random id, never linked to a citizen
# account, never shared with a third party.
#
# A dependency alone cannot set this: FastAPI only copies a dependency's
# Response mutations into the *final* response when the route itself
# returns no Response object of its own. Every route here returns a
# TemplateResponse, so a plain `Depends()` silently drops the cookie
# (confirmed empirically before writing this). Middleware is the correct
# fix -- it wraps the actual outgoing response regardless of its type: it
# resolves/generates the id up front (stashed on request.state so the
# route's own dependency can read it during the same request) and only
# sets the cookie on the way out if it was missing on the way in.
VISITOR_COOKIE = "thirdeye_visitor_id"
_VISITOR_COOKIE_MAX_AGE = 365 * 24 * 60 * 60


class VisitorCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        existing = request.cookies.get(VISITOR_COOKIE)
        request.state.visitor_id = existing or str(uuid.uuid4())
        response = await call_next(request)
        if existing is None:
            response.set_cookie(
                VISITOR_COOKIE, request.state.visitor_id,
                max_age=_VISITOR_COOKIE_MAX_AGE, httponly=True, samesite="lax",
            )
        return response


def get_visitor_id(request: Request) -> str:
    return request.state.visitor_id


VisitorId = Annotated[str, Depends(get_visitor_id)]

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["pct"] = lambda value: f"{float(value) * 100:.1f}%"
templates.env.filters["inr"] = intelligence.format_inr


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


# ---------------------------------------------------------------------------
# Phase 4A -- Citizen accounts. Mirrors CurrentUser/LoggedIn above exactly,
# but reads CITIZEN_SESSION_COOKIE against operations/citizen.py's separate
# citizen_users/citizen_sessions tables.
# ---------------------------------------------------------------------------
class RedirectToCitizenLogin(HTTPException):
    def __init__(self, next_path: str) -> None:
        super().__init__(status_code=303, headers={"Location": f"/citizen/login?next={next_path}"})


def get_current_citizen(request: Request, conn: Conn) -> citizen.CitizenUser | None:
    return citizen.get_session_user(conn, request.cookies.get(CITIZEN_SESSION_COOKIE))


CurrentCitizen = Annotated[citizen.CitizenUser | None, Depends(get_current_citizen)]


def require_citizen_login(request: Request, current_citizen: CurrentCitizen) -> citizen.CitizenUser:
    if current_citizen is None:
        raise RedirectToCitizenLogin(next_path=request.url.path)
    return current_citizen


RequireCitizen = Annotated[citizen.CitizenUser, Depends(require_citizen_login)]


# ---------------------------------------------------------------------------
# Phase 3.4 -- Local Extraction Agent bearer-token auth. Separate from the
# session/role dependencies above: callers are scripts, not browsers, so a
# missing/invalid/revoked key is a 401 JSON response, never a login redirect.
# ---------------------------------------------------------------------------
def get_agent_key(request: Request, conn: Conn) -> agent_auth.AgentKey | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return agent_auth.verify_key(conn, header[7:].strip())


AgentKeyOpt = Annotated[agent_auth.AgentKey | None, Depends(get_agent_key)]


def require_agent_key(agent_key: AgentKeyOpt) -> agent_auth.AgentKey:
    if agent_key is None or not agent_key.active:
        raise HTTPException(status_code=401, detail="invalid or revoked agent key")
    return agent_key


RequireAgentKey = Annotated[agent_auth.AgentKey, Depends(require_agent_key)]
