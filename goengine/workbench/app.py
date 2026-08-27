"""Modules 7 & 11 -- Validation Workbench + Operations Control Center (FastAPI).

Every route requires an authenticated session (Module 11) except /login and
/setup. Write actions carry a specific permission requirement on top of
that. The authenticated username replaces what used to be a free-text
"reviewer"/"annotator"/"added by" form field in Phase 1/2 -- that field was
spoofable by design (anyone could type any name); the session identity is not.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import audit, public, registry, repository, review
from ..certification import calibration as calib
from ..certification import categorize
from ..certification import failures as failure_intel
from ..certification import golden, run_full_certification
from ..certification.sources import certification_summary, certify_source
from ..config import COOKIE_SECURE, Settings
from ..db import init_db
from ..discovery import crawler
from ..extraction import metadata as meta
from ..extraction.text import load_pages
from ..fetching import FetchError
from ..operations import auth
from ..operations import citizen as ops_citizen
from ..operations import departments as ops_departments
from .deps import (
    CITIZEN_SESSION_COOKIE,
    Config,
    Conn,
    CurrentCitizen,
    CurrentUser,
    FetcherDep,
    LoggedIn,
    RequireCertify,
    RequireCitizen,
    RequireEscalate,
    RequireReview,
    SESSION_COOKIE,
    STATIC_DIR,
    get_fetcher,
    templates,
)


def _record_error_redirect(record_id: int, message: str) -> RedirectResponse:
    """A rejected decision (missing core field, empty reason, etc.) is an
    expected, correctable outcome -- not a server error -- so it belongs
    back on the record page as a readable banner, not a raw JSON 400 the
    browser renders as a blank page with no way back."""
    from urllib.parse import quote

    return RedirectResponse(f"/records/{record_id}?error={quote(message)}", status_code=303)


def _post_login_destination(user: auth.User) -> str:
    """Where a role lands after authenticating with no specific `next` in
    hand -- Phase 3.1's role routing. `/` is the public landing page, so it
    is never a sensible post-login target."""
    if user.role == auth.ROLE_REVIEWER:
        return "/ops/review"
    if user.role == auth.ROLE_AUDITOR:
        return "/audit"
    return "/ops/dashboard"  # platform_admin, state_admin, read_only


def _landing_stats(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Real numbers for the (citizen-facing) landing page -- reuses Module
    9's operations summary and Module 7's review counts (the same sources
    of truth as the admin dashboard and /api/verified) rather than a second
    set of queries that could drift out of sync with them.

    Phase 3.2's blueprint asks for citizen-friendly metrics, not technical
    ones -- "records available" here is deliberately the *approved* count
    (what /api/verified actually serves), not the raw ingested count, since
    that is the number a citizen visitor can actually go look at."""
    from ..operations import dashboard as ops_dashboard

    summary = ops_dashboard.operations_summary(conn, settings)
    review_counts = review.counts_by_status(conn)
    return {
        "documents_processed": summary["documents_processed"],
        "projects_published": summary["publication_coverage"]["districts_published"],
        "certified_sources": summary["certified_sources"],
        "districts_covered": summary["active_districts"],
        "records_available": review_counts[review.STATUS_APPROVED],
        "departments_tracked": len(ops_departments.list_departments(conn)),
        "summary": summary,
    }


def _bootstrap_admin_from_env(settings: Settings) -> None:
    """If the DB has no users yet and THIRDEYE_ADMIN_USERNAME/PASSWORD are
    set, create the first platform_admin automatically instead of forcing a
    human through /setup on every deploy. This matters specifically on
    platforms where the DB file doesn't survive a redeploy (e.g. no
    persistent disk actually attached) -- without it, every deploy locks
    everyone out until someone manually re-runs /setup. No-op (falls back to
    the manual /setup flow) if the env vars aren't set, so no default
    credential is ever baked into the app itself."""
    import os

    username = os.environ.get("THIRDEYE_ADMIN_USERNAME")
    password = os.environ.get("THIRDEYE_ADMIN_PASSWORD")
    if not username or not password:
        return

    conn = init_db(settings)
    try:
        if auth.has_any_users(conn):
            return
        try:
            auth.create_user(
                conn, username=username.strip(), password=password,
                role=auth.ROLE_PLATFORM_ADMIN, actor="startup-bootstrap",
            )
        except auth.AuthError:
            pass  # e.g. password too short -- leave it to manual /setup rather than crash startup
    finally:
        conn.close()


def _bootstrap_agent_key_from_env(settings: Settings) -> None:
    """If THIRDEYE_BOOTSTRAP_AGENT_KEY is set, guarantees it's always a
    valid agent key -- the same reasoning as _bootstrap_admin_from_env
    above: Render env vars survive a redeploy even though the database
    doesn't, so the local agent's saved key never goes stale after a reset
    and never needs to be regenerated by hand. No-op if the env var isn't
    set, so no key is ever baked into the app itself."""
    import os

    from ..operations import agent_auth

    token = os.environ.get("THIRDEYE_BOOTSTRAP_AGENT_KEY")
    if not token:
        return

    conn = init_db(settings)
    try:
        agent_auth.ensure_key(conn, token, label="Bootstrap Agent Key", created_by="startup-bootstrap")
    finally:
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.load()
    resolved.ensure_dirs()
    # Create the schema once at startup so a fresh checkout can serve
    # immediately; per-request connections stay short-lived.
    init_db(resolved).close()
    _bootstrap_admin_from_env(resolved)
    _bootstrap_agent_key_from_env(resolved)

    app = FastAPI(title="Thirdeye Operations Control Center", version="0.3.1")
    app.state.settings = resolved
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -----------------------------------------------------------------------
    # Module 11 -- Auth: first-run setup, login, logout
    # -----------------------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request, conn: Conn):
        if auth.has_any_users(conn):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "setup.html", {})

    @app.post("/setup")
    def setup_submit(
        request: Request,
        conn: Conn,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        confirm_password: Annotated[str, Form()],
    ):
        if auth.has_any_users(conn):
            return RedirectResponse("/login", status_code=303)
        if password != confirm_password:
            return templates.TemplateResponse(
                request, "setup.html", {"error": "Passwords do not match"}, status_code=400
            )
        try:
            user_id = auth.create_user(
                conn, username=username.strip(), password=password,
                role=auth.ROLE_PLATFORM_ADMIN, actor="setup",
            )
        except auth.AuthError as exc:
            return templates.TemplateResponse(
                request, "setup.html", {"error": str(exc)}, status_code=400
            )
        user = auth.get_user(conn, user_id)
        token = auth.create_session(conn, user)
        response = RedirectResponse(_post_login_destination(user), status_code=303)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=COOKIE_SECURE)
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, conn: Conn, next: str = ""):
        if not auth.has_any_users(conn):
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"next": next})

    @app.post("/login")
    def login_submit(
        request: Request,
        conn: Conn,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str, Form()] = "",
    ):
        user = auth.authenticate(conn, username.strip(), password)
        if user is None:
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid username or password", "next": next},
                status_code=401,
            )
        token = auth.create_session(conn, user)
        # `next` is only trusted when it's a real deep link a protected page
        # redirected from (require_login sets it to request.url.path); an
        # empty value, or literally "/" -- the public landing page -- means
        # "no specific destination", so route by role instead.
        destination = next if next and next != "/" else _post_login_destination(user)
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=COOKIE_SECURE)
        return response

    @app.post("/logout")
    def logout(request: Request, conn: Conn):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            auth.delete_session(conn, token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # -----------------------------------------------------------------------
    # Phase 3.1 -- public landing page and role-routed /admin entry point
    # -----------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request, conn: Conn, config: Config, current_user: CurrentUser, current_citizen: CurrentCitizen):
        return templates.TemplateResponse(
            request, "landing.html",
            {"current_user": current_user, "current_citizen": current_citizen, "stats": _landing_stats(conn, config)},
        )

    @app.get("/admin")
    def admin_entry(current_user: LoggedIn):
        return RedirectResponse(_post_login_destination(current_user), status_code=303)

    # -----------------------------------------------------------------------
    # Public Government Order browse/search -- the Verified GO Database,
    # open to any visitor. Strictly scoped to approved records (see
    # goengine/public.py); never shares a route or template with the
    # reviewer-only /records/{id}.
    # -----------------------------------------------------------------------
    @app.get("/taluks", response_class=HTMLResponse)
    def public_taluks(request: Request, current_user: CurrentUser, current_citizen: CurrentCitizen):
        return templates.TemplateResponse(
            request, "taluks.html", {"current_user": current_user, "current_citizen": current_citizen},
        )

    @app.get("/villages", response_class=HTMLResponse)
    def public_villages(request: Request, current_user: CurrentUser, current_citizen: CurrentCitizen):
        return templates.TemplateResponse(
            request, "villages.html", {"current_user": current_user, "current_citizen": current_citizen},
        )

    @app.get("/orders", response_class=HTMLResponse)
    def public_orders(
        request: Request,
        conn: Conn,
        current_user: CurrentUser,
        current_citizen: CurrentCitizen,
        department: str | None = None,
        district: str | None = None,
        q: str | None = None,
        page: int = 1,
    ):
        page = max(page, 1)
        limit = 20
        records, total = public.search(
            conn,
            department_bucket=department or None,
            district=district or None,
            q=q or None,
            limit=limit,
            offset=(page - 1) * limit,
        )
        return templates.TemplateResponse(
            request,
            "orders_list.html",
            {
                "current_user": current_user,
                "current_citizen": current_citizen,
                "records": records,
                "total": total,
                "page": page,
                "limit": limit,
                "has_next": page * limit < total,
                "filters": public.filter_options(conn),
                "selected_department": department or "",
                "selected_district": district or "",
                "q": q or "",
            },
        )

    @app.get("/orders/{record_id}", response_class=HTMLResponse)
    def public_order_detail(
        request: Request, record_id: int, conn: Conn, current_user: CurrentUser, current_citizen: CurrentCitizen,
    ):
        record = public.get(conn, record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such Government Order")
        return templates.TemplateResponse(
            request,
            "orders_detail.html",
            {
                "current_user": current_user,
                "current_citizen": current_citizen,
                "record": record,
                "core_fields": meta.CORE_FIELDS,
                "optional_fields": meta.OPTIONAL_FIELDS,
                "is_saved": ops_citizen.is_saved(conn, current_citizen.id, record_id) if current_citizen else False,
            },
        )

    @app.get("/orders/{record_id}/pdf")
    def public_order_pdf(record_id: int, config: Config, conn: Conn):
        located = public.document_id_for(conn, record_id)
        if located is None:
            raise HTTPException(status_code=404, detail="no such Government Order")
        document_id, file_name = located
        payload = repository.read_bytes(config, conn, document_id)
        if payload is None:
            raise HTTPException(status_code=410, detail="file missing from repository")
        safe_name = file_name.replace('"', "")
        return Response(
            content=payload,
            media_type="application/pdf",
            headers={"content-disposition": f'inline; filename="{safe_name}"'},
        )

    # -----------------------------------------------------------------------
    # Phase 4A -- Citizen accounts: registration, login/logout, dashboard,
    # saved searches, bookmarks, and gated downloads. A fully separate
    # identity system from staff auth above -- see CITIZEN_SESSION_COOKIE's
    # docstring in deps.py and schema_citizen.sql's header comment for why.
    # -----------------------------------------------------------------------
    # /register and /citizen/login render the same citizen_auth.html modal --
    # a Log In/Register tab switcher inside one popup, matching a real
    # reference product exactly rather than two separate full pages.
    # default_tab picks which tab/panel starts active; each tab is a real
    # link to the other route (not JS-only), so it degrades correctly
    # without JavaScript and a direct link to either URL always renders the
    # right starting state server-side.
    @app.get("/register", response_class=HTMLResponse)
    def citizen_register_form(request: Request, current_citizen: CurrentCitizen, next: str = ""):
        if current_citizen is not None:
            return RedirectResponse("/dashboard", status_code=303)
        return templates.TemplateResponse(request, "citizen_auth.html", {"next": next, "default_tab": "register"})

    @app.post("/register")
    def citizen_register_submit(
        request: Request,
        conn: Conn,
        full_name: Annotated[str, Form()],
        email: Annotated[str, Form()],
        mobile: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        confirm_password: Annotated[str, Form()] = "",
        terms_accepted: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "",
    ):
        if password != confirm_password:
            return templates.TemplateResponse(
                request, "citizen_auth.html",
                {"error": "Passwords do not match", "next": next, "default_tab": "register"}, status_code=400,
            )
        try:
            citizen_id = ops_citizen.register(
                conn, full_name=full_name, email=email, mobile=mobile or None,
                password=password, terms_accepted=bool(terms_accepted),
            )
        except ops_citizen.CitizenError as exc:
            return templates.TemplateResponse(
                request, "citizen_auth.html",
                {"error": str(exc), "next": next, "default_tab": "register"}, status_code=400,
            )
        new_citizen = ops_citizen.get_citizen(conn, citizen_id)
        token = ops_citizen.create_session(conn, new_citizen)
        destination = next if next and next != "/" else "/dashboard"
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(CITIZEN_SESSION_COOKIE, token, httponly=True, samesite="lax", secure=COOKIE_SECURE)
        return response

    @app.get("/citizen/login", response_class=HTMLResponse)
    def citizen_login_form(request: Request, current_citizen: CurrentCitizen, next: str = ""):
        if current_citizen is not None:
            return RedirectResponse("/dashboard", status_code=303)
        return templates.TemplateResponse(request, "citizen_auth.html", {"next": next, "default_tab": "login"})

    @app.post("/citizen/login")
    def citizen_login_submit(
        request: Request,
        conn: Conn,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str, Form()] = "",
    ):
        found = ops_citizen.authenticate(conn, email, password)
        if found is None:
            return templates.TemplateResponse(
                request, "citizen_auth.html",
                {"error": "Invalid email or password", "next": next, "default_tab": "login"},
                status_code=401,
            )
        token = ops_citizen.create_session(conn, found)
        destination = next if next and next != "/" else "/dashboard"
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(CITIZEN_SESSION_COOKIE, token, httponly=True, samesite="lax", secure=COOKIE_SECURE)
        return response

    @app.post("/citizen/logout")
    def citizen_logout(request: Request, conn: Conn):
        token = request.cookies.get(CITIZEN_SESSION_COOKIE)
        if token:
            ops_citizen.delete_session(conn, token)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(CITIZEN_SESSION_COOKIE)
        return response

    @app.get("/dashboard", response_class=HTMLResponse)
    def citizen_dashboard(request: Request, conn: Conn, current_citizen: RequireCitizen):
        return templates.TemplateResponse(
            request,
            "citizen_dashboard.html",
            {
                "current_citizen": current_citizen,
                "recent_downloads": ops_citizen.recent_downloads(conn, current_citizen.id),
                "saved_records": ops_citizen.list_saved_records(conn, current_citizen.id),
                "saved_searches": ops_citizen.list_saved_searches(conn, current_citizen.id),
            },
        )

    @app.post("/orders/{record_id}/save")
    def citizen_toggle_save(record_id: int, conn: Conn, current_citizen: RequireCitizen):
        if public.get(conn, record_id) is None:
            raise HTTPException(status_code=404, detail="no such Government Order")
        ops_citizen.toggle_saved_record(conn, current_citizen.id, record_id)
        return RedirectResponse(f"/orders/{record_id}", status_code=303)

    @app.post("/dashboard/saved-searches")
    def citizen_save_search(
        conn: Conn,
        current_citizen: RequireCitizen,
        label: Annotated[str, Form()] = "",
        department: Annotated[str, Form()] = "",
        district: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
    ):
        ops_citizen.save_search(
            conn, current_citizen.id, label=label or None,
            department_bucket=department or None, district=district or None, q=q or None,
        )
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/dashboard/saved-searches/{search_id}/delete")
    def citizen_delete_search(search_id: int, conn: Conn, current_citizen: RequireCitizen):
        ops_citizen.delete_saved_search(conn, current_citizen.id, search_id)
        return RedirectResponse("/dashboard", status_code=303)

    def _require_any_login(request: Request, current_user: CurrentUser, current_citizen: CurrentCitizen):
        """Downloads accept *either* audience -- the blueprint says
        'authenticated users', not 'citizens only'. Neither cookie present
        means genuinely anonymous, so send them to register/login."""
        if current_user is None and current_citizen is None:
            raise HTTPException(
                status_code=303, headers={"Location": f"/citizen/login?next={request.url.path}"},
            )
        return current_user, current_citizen

    @app.get("/orders/{record_id}/download/pdf")
    def download_pdf(record_id: int, request: Request, conn: Conn, config: Config, current_user: CurrentUser, current_citizen: CurrentCitizen):
        current_user, current_citizen = _require_any_login(request, current_user, current_citizen)
        located = public.document_id_for(conn, record_id)
        if located is None:
            raise HTTPException(status_code=404, detail="no such Government Order")
        document_id, file_name = located
        payload = repository.read_bytes(config, conn, document_id)
        if payload is None:
            raise HTTPException(status_code=410, detail="file missing from repository")
        ops_citizen.log_download(
            conn, record_id=record_id, format="pdf",
            citizen_id=current_citizen.id if current_citizen else None,
            staff_user_id=current_user.id if current_user else None,
        )
        safe_name = file_name.replace('"', "")
        return Response(
            content=payload, media_type="application/pdf",
            headers={"content-disposition": f'attachment; filename="{safe_name}"'},
        )

    @app.get("/orders/{record_id}/download/text")
    def download_text(record_id: int, request: Request, conn: Conn, current_user: CurrentUser, current_citizen: CurrentCitizen):
        current_user, current_citizen = _require_any_login(request, current_user, current_citizen)
        text = public.get_full_text(conn, record_id)
        if text is None:
            raise HTTPException(status_code=404, detail="no such Government Order")
        ops_citizen.log_download(
            conn, record_id=record_id, format="text",
            citizen_id=current_citizen.id if current_citizen else None,
            staff_user_id=current_user.id if current_user else None,
        )
        return Response(
            content=text, media_type="text/plain",
            headers={"content-disposition": f'attachment; filename="go-{record_id}-extracted-text.txt"'},
        )

    @app.get("/orders/{record_id}/download/metadata")
    def download_metadata(record_id: int, request: Request, conn: Conn, current_user: CurrentUser, current_citizen: CurrentCitizen):
        current_user, current_citizen = _require_any_login(request, current_user, current_citizen)
        record = public.get(conn, record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such Government Order")
        ops_citizen.log_download(
            conn, record_id=record_id, format="metadata",
            citizen_id=current_citizen.id if current_citizen else None,
            staff_user_id=current_user.id if current_user else None,
        )
        payload = json.dumps(asdict(record), indent=2, ensure_ascii=False)
        return Response(
            content=payload, media_type="application/json",
            headers={"content-disposition": f'attachment; filename="go-{record_id}-metadata.json"'},
        )

    # -----------------------------------------------------------------------
    # Validation Workbench dashboard (Phase 1). Lives at /workbench, not /:
    # `/` is the public landing page (Phase 3.1) and must not require login.
    # -----------------------------------------------------------------------
    @app.get("/workbench", response_class=HTMLResponse)
    def dashboard(request: Request, conn: Conn, config: Config, current_user: LoggedIn):
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "sources": registry.list_sources(conn),
                "discovery_counts": crawler.counts_by_status(conn),
                "review_counts": review.counts_by_status(conn),
                "repo_stats": repository.stats(config, conn),
                "pending": review.queue(conn, limit=50),
                "recent_audit": audit.trail(conn, limit=25),
                "current_user": current_user,
            },
        )

    # -----------------------------------------------------------------------
    # Review one record
    # -----------------------------------------------------------------------
    @app.get("/records/{record_id}", response_class=HTMLResponse)
    def record_detail(
        request: Request, record_id: int, conn: Conn, current_user: LoggedIn, error: str | None = None,
    ):
        from ..operations import review as ops_review

        try:
            summary = review.get_summary(conn, record_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        candidates: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute(
            """
            SELECT * FROM go_field_candidates
             WHERE record_id = ?
             ORDER BY field_name, confidence DESC
            """,
            (record_id,),
        ).fetchall():
            candidates.setdefault(row["field_name"], []).append(row)

        return templates.TemplateResponse(
            request,
            "record.html",
            {
                "record": summary,
                "pages": load_pages(conn, summary.extraction_id),
                "core_fields": meta.CORE_FIELDS,
                "optional_fields": meta.OPTIONAL_FIELDS,
                "candidates": candidates,
                "trail": audit.trail(conn, entity_type="go_record", entity_id=record_id),
                "provenance": audit.document_provenance(conn, summary.document_id),
                "current_user": current_user,
                "can_review": current_user.has_permission(auth.PERM_REVIEW_RECORDS),
                "can_escalate": current_user.has_permission(auth.PERM_ESCALATE_RECORDS),
                "escalations": ops_review.escalations_for_record(conn, record_id),
                "error": error,
            },
        )

    # -----------------------------------------------------------------------
    # The original PDF, served from the repository
    # -----------------------------------------------------------------------
    @app.get("/documents/{document_id}/pdf")
    def document_pdf(
        document_id: int, conn: Conn, config: Config, current_user: LoggedIn, download: bool = False,
    ):
        row = conn.execute(
            "SELECT file_name FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        payload = repository.read_bytes(config, conn, document_id)
        if payload is None:
            raise HTTPException(status_code=410, detail="file missing from repository")
        # Default "inline" so the reviewer sees the original beside the
        # extracted fields; ?download=1 (the Document Library's Download
        # link) switches to "attachment" so the browser saves it instead.
        safe_name = row["file_name"].replace('"', "")
        disposition = "attachment" if download else "inline"
        return Response(
            content=payload,
            media_type="application/pdf",
            headers={"content-disposition": f'{disposition}; filename="{safe_name}"'},
        )

    @app.get("/documents/{document_id}/verify")
    def document_verify(document_id: int, conn: Conn, config: Config, current_user: LoggedIn):
        ok, message = repository.verify_document(config, conn, document_id)
        return JSONResponse({"document_id": document_id, "ok": ok, "message": message})

    # -----------------------------------------------------------------------
    # Decisions
    # -----------------------------------------------------------------------
    @app.post("/records/{record_id}/correct")
    def post_correct(
        record_id: int,
        conn: Conn,
        current_user: RequireReview,
        field_name: Annotated[str, Form()],
        new_value: Annotated[str, Form()],
        source_page: Annotated[int | None, Form()] = None,
        note: Annotated[str | None, Form()] = None,
    ):
        try:
            review.correct_field(
                conn, record_id, field_name, new_value.strip(),
                reviewer=current_user.username, source_page=source_page, note=note,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except review.ReviewError as exc:
            return _record_error_redirect(record_id, str(exc))
        return RedirectResponse(f"/records/{record_id}", status_code=303)

    @app.post("/records/{record_id}/approve")
    def post_approve(
        record_id: int,
        conn: Conn,
        current_user: RequireReview,
        note: Annotated[str | None, Form()] = None,
        override: Annotated[str | None, Form()] = None,
    ):
        try:
            review.approve(
                conn, record_id, reviewer=current_user.username, note=note,
                allow_missing_fields=bool(override),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except review.ReviewError as exc:
            # A failed validation (e.g. a core field still missing) is a
            # normal, expected outcome of clicking Approve here -- not a
            # server error, so it belongs back on the record page as a
            # readable banner the reviewer can act on, not a raw JSON 400.
            return _record_error_redirect(record_id, str(exc))
        return RedirectResponse("/workbench", status_code=303)

    @app.post("/records/bulk-approve")
    def post_bulk_approve(
        conn: Conn,
        current_user: RequireReview,
        record_ids: Annotated[list[int], Form()] = [],
        queue: Annotated[str, Form()] = "extraction",
        department: Annotated[str | None, Form()] = None,
    ):
        """Approves several selected records from the Review Center table in
        one action. Each record still goes through the exact same
        review.approve() check as a single approval -- a record missing a
        core field is skipped, not silently force-approved, since that
        safeguard is the entire point of Phase 1's review gate. Skipped
        records stay pending for individual attention; nothing here bypasses
        that override."""
        approved = 0
        skipped: list[tuple[int, str]] = []
        for record_id in record_ids:
            try:
                review.approve(conn, record_id, reviewer=current_user.username)
                approved += 1
            except (review.ReviewError, LookupError) as exc:
                skipped.append((record_id, str(exc)))

        qs = f"queue={queue}"
        if department:
            qs += f"&department={department}"
        qs += f"&bulk_approved={approved}&bulk_skipped={len(skipped)}"
        return RedirectResponse(f"/ops/review?{qs}", status_code=303)

    @app.post("/records/{record_id}/reject")
    def post_reject(
        record_id: int,
        conn: Conn,
        current_user: RequireReview,
        reason: Annotated[str, Form()],
    ):
        try:
            review.reject(conn, record_id, reviewer=current_user.username, reason=reason.strip())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except review.ReviewError as exc:
            return _record_error_redirect(record_id, str(exc))
        return RedirectResponse("/workbench", status_code=303)

    @app.post("/records/{record_id}/escalate")
    def post_escalate(
        record_id: int,
        conn: Conn,
        current_user: RequireEscalate,
        reason: Annotated[str, Form()],
    ):
        from ..operations import review as ops_review

        try:
            ops_review.escalate(conn, record_id, escalated_by=current_user.username, reason=reason.strip())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ops_review.OperationsError as exc:
            return _record_error_redirect(record_id, str(exc))
        return RedirectResponse(f"/records/{record_id}", status_code=303)

    # -----------------------------------------------------------------------
    # Module 3 -- Golden Dataset Workbench
    # -----------------------------------------------------------------------
    @app.get("/golden", response_class=HTMLResponse)
    def golden_list(request: Request, conn: Conn, current_user: LoggedIn):
        return templates.TemplateResponse(
            request,
            "golden_list.html",
            {
                "progress": categorize.acquisition_progress(conn),
                "summary": golden.golden_set_summary(conn),
                "documents": golden.list_golden_documents(conn),
                "candidates": golden.candidates_for_golden_set(conn, limit=30),
                "scored_fields": golden.SCORED_FIELDS,
                "current_user": current_user,
                "can_review": current_user.has_permission(auth.PERM_REVIEW_RECORDS),
            },
        )

    @app.post("/golden/add")
    def golden_add(
        conn: Conn,
        current_user: RequireReview,
        document_id: Annotated[int, Form()],
        notes: Annotated[str | None, Form()] = None,
    ):
        try:
            golden_id = golden.add_to_golden_set(
                conn, document_id, added_by=current_user.username, notes=notes
            )
        except (golden.GoldenSetError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/golden/{golden_id}", status_code=303)

    @app.get("/golden/{golden_document_id}", response_class=HTMLResponse)
    def golden_detail(request: Request, golden_document_id: int, conn: Conn, current_user: LoggedIn):
        try:
            doc = golden.get_golden_document(conn, golden_document_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # The machine's own extracted fields are shown as a reference/starting
        # point only -- the annotation itself is a separate, independently
        # stored judgement, never auto-filled from it.
        record_row = conn.execute(
            "SELECT id FROM go_records WHERE document_id = ? ORDER BY id DESC LIMIT 1",
            (doc.document_id,),
        ).fetchone()
        machine_fields = meta.load_fields(conn, int(record_row["id"])) if record_row else {}

        extraction_row = conn.execute(
            "SELECT id FROM extractions WHERE document_id = ? ORDER BY id DESC LIMIT 1",
            (doc.document_id,),
        ).fetchone()
        pages = load_pages(conn, int(extraction_row["id"])) if extraction_row else []

        return templates.TemplateResponse(
            request,
            "golden_annotate.html",
            {
                "doc": doc,
                "machine_fields": machine_fields,
                "pages": pages,
                "scored_fields": golden.SCORED_FIELDS,
                "all_fields": golden.ALL_GOLDEN_FIELDS,
                "current_user": current_user,
                "can_review": current_user.has_permission(auth.PERM_REVIEW_RECORDS),
            },
        )

    @app.post("/golden/{golden_document_id}/annotate")
    def golden_annotate(
        golden_document_id: int,
        conn: Conn,
        current_user: RequireReview,
        field_name: Annotated[str, Form()],
        value: Annotated[str | None, Form()] = None,
        absent: Annotated[str | None, Form()] = None,
        note: Annotated[str | None, Form()] = None,
    ):
        try:
            golden.annotate_field(
                conn, golden_document_id, field_name, None if absent else value,
                annotator=current_user.username, note=note,
            )
        except (golden.GoldenSetError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/golden/{golden_document_id}", status_code=303)

    # -----------------------------------------------------------------------
    # Module 9 -- Certification Dashboard
    # -----------------------------------------------------------------------
    @app.get("/certification", response_class=HTMLResponse)
    def certification_dashboard(request: Request, conn: Conn, config: Config, current_user: LoggedIn):
        source_rows = conn.execute(
            """
            SELECT id, name, department, url, certification_status, certification_date,
                   last_crawl_success_at, last_crawl_failure_at
              FROM sources
             ORDER BY id
            """
        ).fetchall()

        latest = conn.execute(
            "SELECT * FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_summary = json.loads(latest["summary"]) if latest else None

        ocr_count = conn.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE ocr_applied = 1"
        ).fetchone()["n"]

        calibration_buckets = calib.latest_calibration(conn)
        calibration_error = calib.overall_calibration_error(
            [
                calib.CalibrationBucket(
                    field_name=r["field_name"], bucket_low=r["bucket_low"], bucket_high=r["bucket_high"],
                    predictions_count=r["predictions_count"], correct_count=r["correct_count"],
                    mean_stated_confidence=r["mean_stated_confidence"], actual_accuracy=r["actual_accuracy"],
                )
                for r in calibration_buckets
            ]
        ) if calibration_buckets else None

        return templates.TemplateResponse(
            request,
            "certification.html",
            {
                "source_certification_summary": certification_summary(conn),
                "sources": source_rows,
                "discovery_counts": crawler.counts_by_status(conn),
                "repo_stats": repository.stats(config, conn),
                "ocr_count": ocr_count,
                "golden_summary": golden.golden_set_summary(conn),
                "acquisition_progress": categorize.acquisition_progress(conn),
                "latest_run": latest,
                "latest_summary": latest_summary,
                "run_history": conn.execute(
                    "SELECT id, run_at, documents_scored FROM certification_benchmark_runs ORDER BY id DESC LIMIT 10"
                ).fetchall(),
                "top_failure_types": failure_intel.top_failure_types(conn),
                "failure_trend": failure_intel.failure_trend(conn),
                "department_failures": failure_intel.department_failure_counts(conn),
                "language_failures": failure_intel.language_failure_counts(conn),
                "open_issues": failure_intel.list_failures(conn, limit=25),
                "calibration_buckets": calibration_buckets,
                "calibration_error": calibration_error,
                "current_user": current_user,
                "can_certify": current_user.has_permission(auth.PERM_RUN_CERTIFICATION),
            },
        )

    @app.post("/certification/sources/{source_id}/certify")
    def certify_one_source(source_id: int, conn: Conn, config: Config, fetcher: FetcherDep, current_user: RequireCertify):
        from ..operations import geography
        from ..operations.sources import advance_lifecycle_on_certification

        try:
            result = certify_source(conn, config, fetcher, source_id, actor=current_user.username)
            advance_lifecycle_on_certification(conn, source_id, result.result)
            # A source's certification result is what districts.certification_status
            # is computed from -- refresh every district this source counts
            # toward now, so Publication Control reflects it immediately
            # instead of staying stuck on PENDING until someone separately
            # remembers to click "Refresh Certification" on the Districts page.
            for district_id in geography.districts_affected_by_source(conn, source_id):
                geography.refresh_district_certification(conn, district_id, actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return RedirectResponse("/certification", status_code=303)

    @app.post("/certification/benchmark/run")
    def run_benchmark(conn: Conn, current_user: RequireCertify):
        run_full_certification(conn, actor=current_user.username)
        return RedirectResponse("/certification", status_code=303)

    # -----------------------------------------------------------------------
    # Verified output + audit
    # -----------------------------------------------------------------------
    @app.get("/api/verified")
    def api_verified(conn: Conn, current_user: LoggedIn, limit: int = 500):
        return JSONResponse(review.verified_records(conn, limit=limit))

    @app.get("/api/audit")
    def api_audit(
        conn: Conn,
        current_user: LoggedIn,
        entity_type: str | None = None,
        entity_id: int | None = None,
        limit: int = 200,
    ):
        entries = audit.trail(conn, entity_type=entity_type, entity_id=entity_id, limit=limit)
        return JSONResponse([entry.__dict__ for entry in entries])

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, conn: Conn, current_user: LoggedIn, limit: int = 300, action: str | None = None, entity_type: str | None = None):
        entries = audit.trail(conn, entity_type=entity_type, limit=limit)
        if action:
            entries = [e for e in entries if e.action == action]
        actions = sorted({e.action for e in audit.trail(conn, limit=2000)})
        return templates.TemplateResponse(
            request, "audit.html",
            {
                "entries": entries, "actions": actions, "selected_action": action,
                "selected_entity_type": entity_type, "current_user": current_user,
            },
        )

    _register_operations_routes(app)
    _register_agent_routes(app)

    return app


def _register_operations_routes(app: FastAPI) -> None:
    """Phase 3 routes, split out to keep this module from growing without bound."""
    from . import operations_routes

    operations_routes.register(app)


def _register_agent_routes(app: FastAPI) -> None:
    """Phase 3.4 -- Local Extraction Agent sync API."""
    from . import agent_routes

    agent_routes.register(app)


app = create_app()
