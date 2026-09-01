"""Phase 3 routes: states, districts, departments, versioned sources,
certification jobs, publication, users, system health.

Split out of app.py to keep that module from growing without bound as
Phase 3's admin surface expands -- registered via `register(app, templates)`
from `create_app`. Uses the same Conn/LoggedIn/RequireX dependency pattern
established there (see deps.py).
"""

from __future__ import annotations

import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import audit, registry, repository
from ..db import utcnow
from ..certification.categorize import ALL_BUCKETS, BUCKET_OTHER
from ..certification.sources import certification_history

REVIEW_FILTER_BUCKETS = ALL_BUCKETS + (BUCKET_OTHER,)
from ..operations import auth
from ..operations import backup as ops_backup
from ..operations import dedup as ops_dedup
from ..operations import departments as ops_departments
from ..operations import email as ops_email
from ..operations import geography
from ..operations import dashboard as ops_dashboard
from ..operations import engagement as ops_engagement
from ..operations import health as ops_health
from ..operations import publication as ops_publication
from ..operations import analytics as ops_analytics
from ..operations import quality as ops_quality
from ..operations import reset as ops_reset
from ..operations import review as ops_review
from ..operations import sources as ops_sources
from ..discovery import crawler
from .deps import (
    Config,
    Conn,
    FetcherDep,
    FetcherFactory,
    LoggedIn,
    RequireCertify,
    RequireDepartments,
    RequireDistricts,
    RequireEscalate,
    RequirePublish,
    RequireSources,
    RequireStates,
    RequireUsers,
    templates,
)


def register(app: FastAPI) -> None:
    _register_hub(app)
    _register_states(app)
    _register_districts(app)
    _register_departments(app)
    _register_sources(app)
    _register_jobs(app)
    _register_documents(app)
    _register_review(app)
    _register_publication(app)
    _register_dashboard(app)
    _register_engagement(app)
    _register_health(app)
    _register_users(app)
    _register_agents(app)
    _register_diagnostics(app)
    _register_notifications(app)
    _register_system_reset(app)


# ---------------------------------------------------------------------------
# Admin hub
# ---------------------------------------------------------------------------
def _register_hub(app: FastAPI) -> None:
    @app.get("/ops", response_class=HTMLResponse)
    def ops_hub(request: Request, current_user: LoggedIn):
        return templates.TemplateResponse(request, "ops_hub.html", {"current_user": current_user})


# ---------------------------------------------------------------------------
# Module 1: States
# ---------------------------------------------------------------------------
def _register_states(app: FastAPI) -> None:
    @app.get("/ops/states", response_class=HTMLResponse)
    def states_list(request: Request, conn: Conn, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "states.html",
            {
                "states": geography.list_states(conn),
                "current_user": current_user,
                "can_manage": current_user.has_permission("manage_states"),
                "statuses": geography.STATE_STATUSES,
            },
        )

    @app.post("/ops/states/add")
    def states_add(
        conn: Conn, current_user: RequireStates,
        name: Annotated[str, Form()], code: Annotated[str, Form()],
        launch_date: Annotated[str | None, Form()] = None,
    ):
        try:
            geography.add_state(conn, name=name, code=code, launch_date=launch_date or None, actor=current_user.username)
        except geography.GeographyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/states", status_code=303)

    @app.post("/ops/states/{state_id}/status")
    def states_set_status(
        state_id: int, conn: Conn, current_user: RequireStates, status: Annotated[str, Form()],
    ):
        try:
            geography.set_state_status(conn, state_id, status, actor=current_user.username)
        except (geography.GeographyError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/states", status_code=303)

    @app.post("/ops/states/{state_id}/active")
    def states_set_active(
        state_id: int, conn: Conn, current_user: RequireStates, active: Annotated[str | None, Form()] = None,
    ):
        try:
            geography.set_state_active(conn, state_id, bool(active), actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse("/ops/states", status_code=303)


# ---------------------------------------------------------------------------
# Module 2: Districts
# ---------------------------------------------------------------------------
def _register_districts(app: FastAPI) -> None:
    @app.get("/ops/districts", response_class=HTMLResponse)
    def districts_list(request: Request, conn: Conn, current_user: LoggedIn, state_id: str | None = None):
        state_id = int(state_id) if state_id else None
        can_manage = current_user.has_permission("manage_districts")
        states = geography.list_states(conn)
        # A state admin only sees/edits their own state's districts.
        if current_user.role == "state_admin":
            state_id = current_user.state_id
            states = [s for s in states if s.id == current_user.state_id]

        return templates.TemplateResponse(
            request, "districts.html",
            {
                "districts": geography.list_districts(conn, state_id=state_id),
                "states": states,
                "selected_state_id": state_id,
                "current_user": current_user,
                "can_manage": can_manage,
                "statuses": geography.DISTRICT_STATUSES,
            },
        )

    @app.post("/ops/districts/add")
    def districts_add(
        conn: Conn, current_user: RequireDistricts,
        state_id: Annotated[int, Form()], name: Annotated[str, Form()], code: Annotated[str, Form()],
    ):
        if not current_user.can_act_on_state(state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        try:
            geography.add_district(conn, state_id=state_id, name=name, code=code, actor=current_user.username)
        except (geography.GeographyError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/districts?state_id={state_id}", status_code=303)

    @app.post("/ops/districts/{district_id}/status")
    def districts_set_status(
        district_id: int, conn: Conn, current_user: RequireDistricts, status: Annotated[str, Form()],
    ):
        district = geography.get_district(conn, district_id)
        if district is None:
            raise HTTPException(status_code=404, detail="district not found")
        if not current_user.can_act_on_state(district.state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        try:
            geography.set_district_status(conn, district_id, status, actor=current_user.username)
        except geography.GeographyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/districts", status_code=303)

    @app.post("/ops/districts/{district_id}/refresh-certification")
    def districts_refresh_certification(district_id: int, conn: Conn, current_user: RequireDistricts):
        district = geography.get_district(conn, district_id)
        if district is None:
            raise HTTPException(status_code=404, detail="district not found")
        if not current_user.can_act_on_state(district.state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        geography.refresh_district_certification(conn, district_id, actor=current_user.username)
        return RedirectResponse("/ops/districts", status_code=303)


# ---------------------------------------------------------------------------
# Module 3: Departments
# ---------------------------------------------------------------------------
def _register_departments(app: FastAPI) -> None:
    @app.get("/ops/departments", response_class=HTMLResponse)
    def departments_list(request: Request, conn: Conn, current_user: LoggedIn):
        rows = []
        for department in ops_departments.list_departments(conn):
            rows.append((department, ops_departments.department_metrics(conn, department)))
        return templates.TemplateResponse(
            request, "departments.html",
            {
                "rows": rows, "current_user": current_user,
                "can_manage": current_user.has_permission("manage_departments"),
            },
        )

    @app.post("/ops/departments/add")
    def departments_add(
        conn: Conn, current_user: RequireDepartments,
        name: Annotated[str, Form()], bucket_key: Annotated[str | None, Form()] = None,
    ):
        try:
            ops_departments.add_department(
                conn, name=name, bucket_key=bucket_key or None, actor=current_user.username
            )
        except ops_departments.DepartmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/departments", status_code=303)

    @app.post("/ops/departments/seed")
    def departments_seed(conn: Conn, current_user: RequireDepartments):
        ops_departments.seed(conn, actor=current_user.username)
        return RedirectResponse("/ops/departments", status_code=303)

    @app.post("/ops/departments/{department_id}/active")
    def departments_set_active(
        department_id: int, conn: Conn, current_user: RequireDepartments,
        active: Annotated[str | None, Form()] = None,
    ):
        try:
            ops_departments.set_department_active(conn, department_id, bool(active), actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse("/ops/departments", status_code=303)


# ---------------------------------------------------------------------------
# Modules 4 & 5: Versioned Source Registry + Certification Center
# ---------------------------------------------------------------------------
def _register_sources(app: FastAPI) -> None:
    @app.get("/ops/sources", response_class=HTMLResponse)
    def sources_list(request: Request, conn: Conn, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "sources.html",
            {
                "sources": ops_sources.list_sources_with_geography(conn),
                "states": geography.list_states(conn),
                "districts": geography.list_districts(conn),
                "current_user": current_user,
                "can_manage": current_user.has_permission("manage_sources"),
                "source_types": registry.VALID_SOURCE_TYPES,
                "source_categories": registry.VALID_SOURCE_CATEGORIES,
                "priorities": registry.VALID_PRIORITIES,
                "discovery_methods": ops_sources.DISCOVERY_METHODS,
            },
        )

    @app.post("/ops/sources/add")
    def sources_add(
        conn: Conn, current_user: RequireSources,
        name: Annotated[str, Form()], department: Annotated[str, Form()], url: Annotated[str, Form()],
        source_type: Annotated[str, Form()], discovery_method: Annotated[str | None, Form()] = None,
        state_id: Annotated[int | None, Form()] = None, district_id: Annotated[int | None, Form()] = None,
        priority: Annotated[str, Form()] = "Medium", source_category: Annotated[str | None, Form()] = None,
        ssl_verification_enabled: Annotated[int, Form()] = 1, ssl_fallback: Annotated[int, Form()] = 0,
    ):
        if state_id is not None and not current_user.can_act_on_state(state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        try:
            source_id = ops_sources.create_source(
                conn, name=name, department=department, url=url, source_type=source_type,
                discovery_method=discovery_method or None, state_id=state_id, district_id=district_id,
                priority=priority, source_category=source_category or None,
                ssl_verification_enabled=bool(ssl_verification_enabled),
                allow_ssl_fallback=bool(ssl_fallback),
                actor=current_user.username,
            )
        except (registry.SourceRejected, ops_sources.SourceOperationsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{source_id}", status_code=303)

    @app.post("/ops/sources/{source_id}/priority")
    def sources_set_priority(
        source_id: int, conn: Conn, current_user: RequireSources, priority: Annotated[str, Form()],
    ):
        try:
            registry.set_priority(conn, source_id, priority, actor=current_user.username)
        except (registry.SourceRejected, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{source_id}", status_code=303)

    @app.get("/ops/sources/{source_id}", response_class=HTMLResponse)
    def source_detail(request: Request, source_id: int, conn: Conn, current_user: LoggedIn):
        source = conn.execute(
            """
            SELECT s.*, st.name AS state_name, d.name AS district_name
              FROM sources s LEFT JOIN states st ON st.id = s.state_id LEFT JOIN districts d ON d.id = s.district_id
             WHERE s.id = ?
            """,
            (source_id,),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return templates.TemplateResponse(
            request, "source_detail.html",
            {
                "source": source,
                "versions": ops_sources.version_history(conn, source_id),
                "certifications": certification_history(conn, source_id),
                "current_user": current_user,
                "can_manage": current_user.has_permission("manage_sources"),
                "can_certify": current_user.has_permission("run_certification"),
                "discovery_methods": ops_sources.DISCOVERY_METHODS,
                "priorities": registry.VALID_PRIORITIES,
            },
        )

    @app.post("/ops/sources/{source_id}/edit")
    def sources_edit(
        source_id: int, conn: Conn, current_user: RequireSources,
        reason: Annotated[str, Form()],
        name: Annotated[str | None, Form()] = None, department: Annotated[str | None, Form()] = None,
        url: Annotated[str | None, Form()] = None, discovery_method: Annotated[str | None, Form()] = None,
    ):
        try:
            ops_sources.edit_source(
                conn, source_id, name=name or None, department=department or None, url=url or None,
                discovery_method=discovery_method or None, actor=current_user.username, reason=reason,
            )
        except (ops_sources.SourceOperationsError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{source_id}", status_code=303)

    @app.post("/ops/sources/{source_id}/test")
    def sources_test(source_id: int, conn: Conn, current_user: RequireSources, fetcher: FetcherDep):
        try:
            ops_sources.quick_test_source(conn, fetcher, source_id, actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{source_id}", status_code=303)

    @app.post("/ops/sources/{source_id}/retire")
    def sources_retire(
        source_id: int, conn: Conn, current_user: RequireSources, reason: Annotated[str, Form()],
    ):
        try:
            ops_sources.retire_source(conn, source_id, actor=current_user.username, reason=reason)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{source_id}", status_code=303)

    @app.post("/ops/sources/{source_id}/clone")
    def sources_clone(source_id: int, conn: Conn, current_user: RequireSources):
        try:
            new_id = ops_sources.clone_source(conn, source_id, actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{new_id}", status_code=303)


# ---------------------------------------------------------------------------
# Module 6: Certification Job Center
# ---------------------------------------------------------------------------
def _register_jobs(app: FastAPI) -> None:
    from datetime import datetime, timedelta, timezone
    from ..operations import agent_auth, extraction_queue

    # An agent whose key was used within this window counts as "online" --
    # matches the daemon's own default poll interval (see cli.py
    # cmd_agent_daemon), so a normally-polling agent never flickers offline
    # between requests.
    AGENT_ONLINE_WINDOW_SECONDS = 90

    def _agent_status(conn: sqlite3.Connection) -> dict:
        keys = agent_auth.list_keys(conn)
        now = datetime.now(timezone.utc)
        online = False
        for k in keys:
            if k.active and k.last_used_at:
                try:
                    seen = datetime.fromisoformat(k.last_used_at)
                except ValueError:
                    continue
                if now - seen <= timedelta(seconds=AGENT_ONLINE_WINDOW_SECONDS):
                    online = True
                    break
        local_requests = extraction_queue.list_requests(conn, limit=10)
        return {
            "agent_online": online,
            "agent_key_count": len([k for k in keys if k.active]),
            "queue_size": extraction_queue.queue_size(conn),
            "local_requests": [
                {**dict(r), **extraction_queue.run_history_row(conn, r)} for r in local_requests
            ],
            "agent_ops": extraction_queue.agent_operations_status(conn),
        }

    @app.get("/ops/jobs", response_class=HTMLResponse)
    def jobs_list(request: Request, conn: Conn, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "jobs.html",
            {
                "states": geography.list_states(conn),
                "current_user": current_user,
                "can_run": current_user.has_permission("run_certification"),
                "departments": registry.list_departments(conn),
                **_agent_status(conn),
            },
        )

    @app.post("/ops/jobs/start")
    def jobs_start(
        request: Request, conn: Conn, current_user: RequireCertify,
        state_id: Annotated[int | None, Form()] = None,
        district_id: Annotated[int | None, Form()] = None,
        departments: Annotated[list[str] | None, Form()] = None,
    ):
        if state_id is not None and not current_user.can_act_on_state(state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")

        extraction_queue.enqueue_local_request(
            conn, state_id=state_id, district_id=district_id,
            department_filter=departments or None, created_by=current_user.username,
        )
        return RedirectResponse("/ops/jobs", status_code=303)

    @app.post("/ops/jobs/resync-all")
    def jobs_resync_all(conn: Conn, current_user: RequireCertify):
        """Manual trigger for the same resync_all request a production reset
        queues automatically (operations/reset.py) -- for recovering
        documents that were already corrupted by a reset that ran before
        that automatic queuing existed, or any other time an admin suspects
        the local agent's sync bookkeeping is stale. Picked up by the local
        agent's existing scheduled poll; no manual command required."""
        extraction_queue.enqueue_resync_all_request(conn, created_by=current_user.username)
        return RedirectResponse("/ops/jobs", status_code=303)

    @app.post("/ops/jobs/{request_id}/cancel")
    def jobs_cancel(request_id: int, conn: Conn, current_user: RequireCertify):
        """Marks a stuck/no-longer-wanted local extraction request as
        failed from the dashboard -- for exactly the scenario a real
        production run hit: the local agent hung or was force-stopped and
        will never report back on its own. Doesn't reach the agent itself
        (see extraction_queue.cancel_request's own docstring) -- if it's
        actually still running, it must also be stopped on that machine."""
        try:
            extraction_queue.cancel_request(conn, request_id, actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/jobs", status_code=303)


# ---------------------------------------------------------------------------
# Document Library -- every downloaded file, independent of review status,
# with a download link. Composition only: reads through repository.py and
# the same tables the review workbench and dashboard already use.
# ---------------------------------------------------------------------------
def _register_documents(app: FastAPI) -> None:
    DOCUMENTS_PAGE_SIZE = 50

    @app.get("/ops/documents", response_class=HTMLResponse)
    def documents_list(
        request: Request, conn: Conn, current_user: LoggedIn,
        source_id: str | None = None, q: str | None = None, department: str | None = None,
        year: str | None = None, language: str | None = None, status: str | None = None,
        page: int = 1,
    ):
        source_id_int = int(source_id) if source_id else None
        year_int = int(year) if year else None
        page = max(page, 1)
        filters = dict(
            source_id=source_id_int, search=q, department=department,
            year=year_int, language=language, status=status,
        )
        total = repository.count_documents(conn, **filters)
        total_pages = max((total + DOCUMENTS_PAGE_SIZE - 1) // DOCUMENTS_PAGE_SIZE, 1)
        page = min(page, total_pages)
        qs = urlencode({
            k: v for k, v in {
                "source_id": source_id_int, "q": q, "department": department,
                "year": year_int, "language": language, "status": status,
            }.items() if v
        })
        return templates.TemplateResponse(
            request, "documents.html",
            {
                "documents": repository.list_documents(
                    conn, **filters, limit=DOCUMENTS_PAGE_SIZE, offset=(page - 1) * DOCUMENTS_PAGE_SIZE,
                ),
                "total_documents": total,
                "page": page,
                "total_pages": total_pages,
                "page_size": DOCUMENTS_PAGE_SIZE,
                "pagination_qs": qs,
                "sources": registry.list_sources(conn),
                "departments": repository.list_document_departments(conn),
                "years": repository.list_document_years(conn),
                "languages": ("english", "tamil", "mixed", "unknown"),
                "statuses": ("approved", "rejected", "pending", "new", "downloaded", "parsed", "verified"),
                "selected_source_id": source_id_int,
                "search": q or "",
                "selected_department": department or "",
                "selected_year": year_int,
                "selected_language": language or "",
                "selected_status": status or "",
                "current_user": current_user,
                "backfilled": request.query_params.get("backfilled"),
                "backfill_missing": request.query_params.get("backfill_missing"),
            },
        )

    @app.post("/ops/documents/backfill-blobs")
    def documents_backfill_blobs(conn: Conn, config: Config, current_user: RequireSources):
        """One-time (repeatable) repair for documents synced before durable
        blob storage existed: copies whatever's still on local disk into
        Turso. Anything already missing from disk can't be recovered here --
        it needs a genuine re-crawl from source. See repository.py's
        backfill_blobs_from_disk for the sweep itself."""
        result = repository.backfill_blobs_from_disk(config, conn)
        return RedirectResponse(
            f"/ops/documents?backfilled={len(result['backfilled'])}&backfill_missing={len(result['missing_on_disk'])}",
            status_code=303,
        )


# ---------------------------------------------------------------------------
# Module 7: Review Workbench (typed queues + escalation)
# ---------------------------------------------------------------------------
def _register_review(app: FastAPI) -> None:
    REVIEW_PAGE_SIZE = 50

    @app.get("/ops/review", response_class=HTMLResponse)
    def review_hub(
        request: Request, conn: Conn, current_user: LoggedIn,
        queue: str = ops_review.QUEUE_EXTRACTION, department: str | None = None, page: int = 1,
    ):
        if queue not in (ops_review.QUEUE_EXTRACTION, ops_review.QUEUE_OCR, ops_review.QUEUE_METADATA, ops_review.QUEUE_FAILURE):
            raise HTTPException(status_code=400, detail="unknown queue type")
        department = department or None
        if department is not None and department not in REVIEW_FILTER_BUCKETS:
            raise HTTPException(status_code=400, detail="unknown department")

        counts = ops_review.queue_counts(conn, department=department)
        total = counts[queue]
        total_pages = max((total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE, 1)
        page = min(max(page, 1), total_pages)
        qs = urlencode({"queue": queue, **({"department": department} if department else {})})

        return templates.TemplateResponse(
            request, "review_hub.html",
            {
                "counts": counts,
                "selected_queue": queue,
                "selected_department": department or "",
                "departments": REVIEW_FILTER_BUCKETS,
                "records": ops_review.queue_by_type(
                    conn, queue, department=department, limit=REVIEW_PAGE_SIZE,
                    offset=(page - 1) * REVIEW_PAGE_SIZE,
                ),
                "page": page,
                "total_pages": total_pages,
                "page_size": REVIEW_PAGE_SIZE,
                "pagination_qs": qs,
                "open_escalations": ops_review.open_escalations(conn),
                "current_user": current_user,
                "can_escalate": current_user.has_permission("escalate_records"),
                "bulk_approved": request.query_params.get("bulk_approved"),
                "bulk_skipped": request.query_params.get("bulk_skipped"),
            },
        )

    @app.get("/ops/review/duplicates", response_class=HTMLResponse)
    def review_duplicates(request: Request, conn: Conn, current_user: LoggedIn):
        """Report: which documents currently have more than one 'pending'
        go_records row, almost certainly from retrying a sync before that
        was fixed to stop happening. The cleanup action below it is opt-in
        and requires its own confirmation -- viewing this page alone never
        changes anything."""
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        return templates.TemplateResponse(
            request, "review_duplicates.html",
            {
                "summary": ops_dedup.duplicate_summary(conn),
                "current_user": current_user,
                "cleaned": request.query_params.get("cleaned"),
                "cleaned_docs": request.query_params.get("cleaned_docs"),
            },
        )

    @app.post("/ops/review/duplicates/cleanup")
    def review_duplicates_cleanup(
        conn: Conn, current_user: LoggedIn, confirm: Annotated[str | None, Form()] = None,
    ):
        """Actually removes the duplicate pending records the report above
        lists. Requires the confirmation checkbox -- this permanently
        deletes rows, even though it never touches an approved/rejected
        record."""
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if confirm != "yes":
            raise HTTPException(status_code=400, detail="confirmation required")
        result = ops_dedup.run_cleanup(conn, actor=current_user.username)
        return RedirectResponse(
            f"/ops/review/duplicates?cleaned={result['records_removed']}&cleaned_docs={result['documents_cleaned']}",
            status_code=303,
        )

    @app.post("/ops/review/escalations/{escalation_id}/resolve")
    def resolve_escalation(
        escalation_id: int, conn: Conn, current_user: RequireEscalate,
        note: Annotated[str | None, Form()] = None,
    ):
        try:
            ops_review.resolve_escalation(conn, escalation_id, resolved_by=current_user.username, note=note)
        except (ops_review.OperationsError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/review", status_code=303)


# ---------------------------------------------------------------------------
# Module 8: Publication Control Center
# ---------------------------------------------------------------------------
def _register_publication(app: FastAPI) -> None:
    @app.get("/ops/publication", response_class=HTMLResponse)
    def publication_hub(request: Request, conn: Conn, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "publication.html",
            {
                "districts": geography.list_districts(conn),
                "departments": ops_departments.list_departments(conn),
                "coverage": ops_publication.publication_coverage(conn),
                "current_user": current_user,
                "can_publish": current_user.has_permission("publish"),
            },
        )

    @app.post("/ops/publication/districts/{district_id}/publish")
    def publish_district(district_id: int, conn: Conn, current_user: RequirePublish):
        district = geography.get_district(conn, district_id)
        if district is None:
            raise HTTPException(status_code=404, detail="district not found")
        if not current_user.can_act_on_state(district.state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        try:
            ops_publication.publish_district(conn, district_id, actor=current_user.username)
        except ops_publication.PublicationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/publication", status_code=303)

    @app.post("/ops/publication/districts/{district_id}/unpublish")
    def unpublish_district(
        district_id: int, conn: Conn, current_user: RequirePublish, reason: Annotated[str, Form()],
    ):
        district = geography.get_district(conn, district_id)
        if district is None:
            raise HTTPException(status_code=404, detail="district not found")
        if not current_user.can_act_on_state(district.state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        try:
            ops_publication.unpublish_district(conn, district_id, actor=current_user.username, reason=reason)
        except ops_publication.PublicationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/publication", status_code=303)

    @app.post("/ops/publication/departments/{department_id}/publish")
    def publish_department(department_id: int, conn: Conn, current_user: RequirePublish):
        try:
            ops_publication.publish_department(conn, department_id, actor=current_user.username)
        except (ops_publication.PublicationError, LookupError) as exc:
            status = 404 if isinstance(exc, LookupError) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return RedirectResponse("/ops/publication", status_code=303)

    @app.post("/ops/publication/departments/{department_id}/unpublish")
    def unpublish_department(
        department_id: int, conn: Conn, current_user: RequirePublish, reason: Annotated[str, Form()],
    ):
        try:
            ops_publication.unpublish_department(conn, department_id, actor=current_user.username, reason=reason)
        except (ops_publication.PublicationError, LookupError) as exc:
            status = 404 if isinstance(exc, LookupError) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return RedirectResponse("/ops/publication", status_code=303)


# ---------------------------------------------------------------------------
# Module 9: Operations Dashboard
# ---------------------------------------------------------------------------
def _register_dashboard(app: FastAPI) -> None:
    @app.get("/ops/dashboard", response_class=HTMLResponse)
    def operations_dashboard(request: Request, conn: Conn, config: Config, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "ops_dashboard.html",
            {
                "summary": ops_dashboard.operations_summary(conn, config),
                "alerts": ops_health.system_health(conn, config)["alerts"],
                "recent_activity": audit.trail(conn, limit=8),
                "current_user": current_user,
            },
        )

    @app.get("/ops/quality", response_class=HTMLResponse)
    def extraction_quality(request: Request, conn: Conn, config: Config, current_user: LoggedIn):
        health = ops_quality.department_health(conn, config)
        department_kpis = ops_quality.department_coverage_kpis(conn, registry.list_departments(conn), health)
        return templates.TemplateResponse(
            request, "ops_quality.html",
            {
                "quality": ops_quality.quality_summary(conn),
                "department_health": health,
                "department_kpis": department_kpis,
                "coverage": ops_quality.extraction_coverage(conn, department_kpis),
                "current_user": current_user,
            },
        )

    @app.get("/ops/metadata-workbench", response_class=HTMLResponse)
    def metadata_workbench(
        request: Request, conn: Conn, config: Config, current_user: LoggedIn, department: str | None = None,
    ):
        return templates.TemplateResponse(
            request, "ops_metadata_workbench.html",
            {
                "queue": ops_quality.missing_metadata_queue(conn, config, department=department or None),
                "departments": registry.list_departments(conn),
                "selected_department": department or "",
                "current_user": current_user,
            },
        )

    @app.get("/ops/repository", response_class=HTMLResponse)
    def repository_excellence(request: Request, conn: Conn, config: Config, current_user: LoggedIn):
        """Phase 3.6 -- is the published repository itself healthy/complete/
        trustworthy, distinct from /ops/quality's "is extraction producing
        good data." Composes analytics.py + quality.py; the two aren't
        cross-imported (analytics.py already imports from quality.py), so
        the by-year trend is combined here rather than inside
        repository_health() itself."""
        analytics_data = ops_analytics.repository_analytics(conn, config)
        return templates.TemplateResponse(
            request, "ops_repository.html",
            {
                "readiness": ops_quality.department_readiness(conn, config),
                "analytics": analytics_data,
                "health": ops_quality.repository_health(conn, config),
                "trend": analytics_data["by_year"],
                "current_user": current_user,
            },
        )


# ---------------------------------------------------------------------------
# Third Eye 4.1.1 -- Citizen Experience Validation: engagement analytics
# ---------------------------------------------------------------------------
def _register_engagement(app: FastAPI) -> None:
    @app.get("/ops/engagement", response_class=HTMLResponse)
    def engagement_report(
        request: Request, conn: Conn, current_user: LoggedIn, days: int = 30,
        purged: str | None = None,
    ):
        days = days if days in (7, 30, 90) else 30
        top_records = ops_engagement.top_viewed_records(conn, days=days, limit=10)
        return templates.TemplateResponse(
            request, "engagement.html",
            {
                "current_user": current_user,
                "can_purge": current_user.has_permission("manage_sources"),
                "selected_days": days,
                "session_metrics": ops_engagement.session_metrics(conn, days=days),
                "total_downloads": ops_engagement.total_downloads(conn, days=days),
                "district_popularity": ops_engagement.district_popularity(conn, days=days),
                "category_popularity": ops_engagement.category_popularity(conn, days=days),
                "timeline_vs_search": ops_engagement.timeline_vs_search(conn, days=days),
                "average_timeline_depth": ops_engagement.average_timeline_depth(conn, days=days),
                "feature_usage": ops_engagement.feature_usage(conn, days=days),
                "top_records": top_records,
                "most_viewed": top_records[0] if top_records else None,
                "purged": purged,
            },
        )

    @app.post("/ops/engagement/purge")
    def engagement_purge(conn: Conn, current_user: LoggedIn, confirm: Annotated[str | None, Form()] = None):
        """Removes engagement_events older than 12 months. Same
        confirmation-checkbox pattern as /ops/review/duplicates/cleanup --
        deliberate, audited, and manual, never automatic. Touches only the
        one table (see engagement.purge_old_events's docstring)."""
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if confirm != "yes":
            raise HTTPException(status_code=400, detail="confirmation required")
        removed = ops_engagement.purge_old_events(conn, months=12)
        audit.record(
            conn, action="engagement.purged", entity_type="system", entity_id=None,
            actor=current_user.username, detail={"rows_removed": removed},
        )
        return RedirectResponse(f"/ops/engagement?purged={removed}", status_code=303)


# ---------------------------------------------------------------------------
# Module 12: System Health Center
# ---------------------------------------------------------------------------
def _register_health(app: FastAPI) -> None:
    @app.get("/ops/health", response_class=HTMLResponse)
    def health_page(request: Request, conn: Conn, config: Config, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "system_health.html",
            {
                "health": ops_health.system_health(conn, config),
                "source_health": ops_health.source_health_table(conn),
                "current_user": current_user,
            },
        )

    @app.get("/api/health")
    def health_api(conn: Conn, config: Config, current_user: LoggedIn):
        return JSONResponse(ops_health.system_health(conn, config))


# ---------------------------------------------------------------------------
# Module 11: User & Role Management (admin UI over auth.py)
# ---------------------------------------------------------------------------
def _register_users(app: FastAPI) -> None:
    @app.get("/ops/users", response_class=HTMLResponse)
    def users_list(request: Request, conn: Conn, current_user: LoggedIn):
        users = auth.list_users(conn)
        states = {s.id: s.name for s in geography.list_states(conn)}
        return templates.TemplateResponse(
            request, "users.html",
            {
                "users": users, "states_by_id": states, "states": geography.list_states(conn),
                "current_user": current_user, "can_manage": current_user.has_permission(auth.PERM_MANAGE_USERS),
                "roles": auth.ALL_ROLES,
            },
        )

    @app.post("/ops/users/add")
    def users_add(
        conn: Conn, current_user: RequireUsers,
        username: Annotated[str, Form()], password: Annotated[str, Form()], role: Annotated[str, Form()],
        state_id: Annotated[int | None, Form()] = None,
    ):
        try:
            auth.create_user(
                conn, username=username.strip(), password=password, role=role,
                state_id=state_id, actor=current_user.username,
            )
        except auth.AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/ops/users", status_code=303)

    @app.post("/ops/users/{user_id}/active")
    def users_set_active(
        user_id: int, conn: Conn, current_user: RequireUsers, active: Annotated[str | None, Form()] = None,
    ):
        if user_id == current_user.id and not active:
            raise HTTPException(status_code=400, detail="you cannot deactivate your own account")
        try:
            auth.set_user_active(conn, user_id, bool(active), actor=current_user.username)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse("/ops/users", status_code=303)


# ---------------------------------------------------------------------------
# Phase 3.4: Local Extraction Agent key management + sync log
# ---------------------------------------------------------------------------
def _register_agents(app: FastAPI) -> None:
    from ..operations import agent_auth
    from ..certification.categorize import ALL_BUCKETS

    def _agents_context(request: Request, conn, current_user, *, new_token: str | None = None):
        sync_log = conn.execute(
            """
            SELECT l.*, s.name AS source_name, d.file_name, r.id AS record_id
              FROM agent_sync_log l
              LEFT JOIN sources s ON s.id = l.source_id
              LEFT JOIN documents d ON d.id = l.document_id
              LEFT JOIN go_records r ON r.id = l.go_record_id
             ORDER BY l.id DESC LIMIT 200
            """
        ).fetchall()
        selected_department = request.query_params.get("department") or ""
        server_url = f"{request.url.scheme}://{request.url.netloc}"
        dept_flag = f" --department {selected_department}" if selected_department else ""
        return {
            "keys": agent_auth.list_keys(conn),
            "sync_log": sync_log,
            "current_user": current_user,
            "can_manage": current_user.has_permission(auth.PERM_MANAGE_USERS),
            "new_token": new_token,
            "departments": ALL_BUCKETS,
            "selected_department": selected_department,
            "server_url": server_url,
            "run_command": f"thirdeye run --data-dir thirdeye-local{dept_flag}",
            "sync_command": f"thirdeye sync --data-dir thirdeye-local --server-url {server_url}{dept_flag}",
        }

    @app.get("/ops/agents", response_class=HTMLResponse)
    def agents_list(request: Request, conn: Conn, current_user: RequireUsers):
        return templates.TemplateResponse(request, "agents.html", _agents_context(request, conn, current_user))

    @app.post("/ops/agents/add", response_class=HTMLResponse)
    def agents_add(request: Request, conn: Conn, current_user: RequireUsers, label: Annotated[str, Form()]):
        try:
            _, token = agent_auth.generate_key(conn, label=label, created_by=current_user.username)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request, "agents.html", _agents_context(request, conn, current_user, new_token=token)
        )

    @app.post("/ops/agents/{agent_key_id}/revoke")
    def agents_revoke(agent_key_id: int, conn: Conn, current_user: RequireUsers):
        agent_auth.revoke_key(conn, agent_key_id, actor=current_user.username)
        return RedirectResponse("/ops/agents", status_code=303)


# ---------------------------------------------------------------------------
# Diagnostics & Observability endpoints
# ---------------------------------------------------------------------------
def _register_diagnostics(app: FastAPI) -> None:
    @app.post("/ops/sources/{source_id}/ssl")
    def sources_set_ssl(
        source_id: int,
        conn: Conn,
        current_user: RequireSources,
        ssl_verification_enabled: Annotated[str | None, Form()] = None,
        allow_ssl_fallback: Annotated[str | None, Form()] = None,
    ):
        try:
            registry.set_ssl_config(
                conn,
                source_id,
                ssl_verification_enabled=bool(ssl_verification_enabled),
                allow_ssl_fallback=bool(allow_ssl_fallback),
                actor=current_user.username,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(f"/ops/sources/{source_id}", status_code=303)

    @app.post("/ops/settings/retention")
    def update_retention(
        conn: Conn,
        current_user: LoggedIn,
        retention_days: Annotated[int, Form()],
    ):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('diagnostics_retention_days', ?)",
            (str(retention_days),)
        )
        return RedirectResponse("/ops/diagnostics", status_code=303)

    @app.get("/ops/diagnostics", response_class=HTMLResponse)
    def diagnostics_hub(request: Request, conn: Conn, current_user: LoggedIn):
        row = conn.execute("SELECT value FROM system_settings WHERE key = 'diagnostics_retention_days'").fetchone()
        retention_days = int(row["value"]) if row else 30

        sources_data = conn.execute(
            """
            SELECT s.id, s.name, s.priority, s.last_crawl_at, s.last_crawl_status,
                   c.status AS run_status,
                   c.pages_visited, c.links_seen AS links_found,
                   c.dept_pages_found, c.go_listings_found, c.pdf_links_found,
                   c.downloaded_count, c.parsed_count, c.ocr_count,
                   c.download_failures + c.parser_failures AS failed_documents,
                   c.error AS last_error
              FROM sources s
              LEFT JOIN crawl_runs c ON c.source_id = s.id AND c.id = (
                  SELECT MAX(cr.id) FROM crawl_runs cr WHERE cr.source_id = s.id
              )
             WHERE s.active = 1
             ORDER BY s.priority = 'Critical' DESC, s.priority = 'High' DESC, s.name
            """
        ).fetchall()

        sources_list = []
        for s in sources_data:
            last_run = s["last_crawl_at"]
            last_status = s["last_crawl_status"]
            downloaded = s["downloaded_count"] or 0
            found = s["pdf_links_found"] or 0
            failed = s["failed_documents"] or 0
            reject_rate = (failed / found) if found else 0.0

            score = 0
            score += 50 if last_status in (None, "ok") else 0
            score += 30 if downloaded > 0 else 0
            score += 20 if reject_rate < 0.20 else 0

            if last_run is None:
                status = "Offline"
            elif score >= 80:
                status = "Healthy"
            elif score >= 50:
                status = "Warning"
            elif score >= 20:
                status = "Critical"
            else:
                status = "Offline"

            sources_list.append({
                "id": s["id"],
                "name": s["name"],
                "priority": s["priority"],
                "last_run": last_run,
                "run_status": s["run_status"] or "never",
                "pages_visited": s["pages_visited"] or 0,
                "links_found": s["links_found"] or 0,
                "dept_pages_found": s["dept_pages_found"] or 0,
                "go_listings_found": s["go_listings_found"] or 0,
                "pdf_links_found": s["pdf_links_found"] or 0,
                "downloaded_count": downloaded,
                "parsed_count": s["parsed_count"] or 0,
                "ocr_count": s["ocr_count"] or 0,
                "failed_documents": failed,
                "last_error": s["last_error"],
                "health_score": score,
                "status": status,
            })

        return templates.TemplateResponse(
            request, "diagnostics_hub.html",
            {
                "sources": sources_list,
                "retention_days": retention_days,
                "current_user": current_user,
                "can_manage": current_user.has_permission("manage_sources"),
            }
        )

    @app.post("/ops/sources/{source_id}/diagnose")
    def run_source_diagnostic(source_id: int, conn: Conn, current_user: LoggedIn, fetcher_factory: FetcherFactory):
        source = registry.get_source(conn, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")

        fetcher = fetcher_factory()
        try:
            crawl_result = crawler.crawl_source(conn, fetcher, source, actor="diagnostic", max_pages=3)
            
            run_id = crawl_result.run_id
            run_data = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
            evidences = conn.execute("SELECT * FROM crawl_evidences WHERE crawl_run_id = ?", (run_id,)).fetchall()
            response_times = [r["response_time_ms"] for r in evidences if r["response_time_ms"] is not None]
            avg_resp = (sum(response_times) / (1000.0 * len(response_times))) if response_times else 0.0
            
            report_lines = []
            report_lines.append(f"DIAGNOSTIC REPORT FOR SOURCE: {source.name}")
            report_lines.append(f"Run At: {run_data['started_at']}")
            report_lines.append(f"Run Status: {run_data['status']}")
            report_lines.append(f"Pages Visited: {run_data['pages_visited']}")
            report_lines.append(f"Links Found: {run_data['links_seen']}")
            report_lines.append(f"Department Pages Found: {run_data['dept_pages_found']}")
            report_lines.append(f"GO Listings Found: {run_data['go_listings_found']}")
            report_lines.append(f"PDF Links Found: {run_data['pdf_links_found']}")
            report_lines.append(f"Downloaded: {run_data['downloaded_count']}")
            report_lines.append(f"Parsed: {run_data['parsed_count']}")
            report_lines.append(f"Needs OCR: {run_data['ocr_count']}")
            report_lines.append(f"Failed: {run_data['parser_failures'] + run_data['download_failures']}")
            report_lines.append(f"Average Response Time: {avg_resp:.2f}s")
            report_lines.append(f"Last Error: {run_data['error'] or 'None'}")
            report_lines.append("\n=== Visited URL Evidences ===")
            for ev in evidences:
                lines = [
                    f"Target URL: {ev['url']}",
                    f"  HTTP Status: {ev['status_code']}",
                    f"  Size: {ev['response_size']} bytes",
                    f"  Duration: {ev['duration_ms']:.1f}ms",
                    f"  Redirects: {ev['redirect_count']}",
                    f"  SSL Verified: {ev['ssl_verified']}",
                ]
                if ev["failure_category"]:
                    # The eight fields the Network Truth Framework requires
                    # every diagnostic report to state explicitly.
                    lines += [
                        f"  Last Successful Stage: {ev['last_successful_stage'] or '(none)'}",
                        f"  Failure Stage: {ev['failure_stage'] or 'None'}",
                        f"  Root Cause Category: {ev['failure_category']}",
                        f"  Technical Evidence: {ev['error_message'] or 'None'}",
                        f"  Exception Type: {ev['exception_type'] or 'None'}",
                        f"  Exception Message: {ev['exception_message'] or 'None'}",
                        f"  Confidence Level: {ev['confidence_level'] or 'None'}",
                    ]
                report_lines.append("\n".join(lines) + "\n")
            
            report_text = "\n".join(report_lines)
            
            cur = conn.execute(
                """
                INSERT INTO diagnostic_reports
                    (source_id, run_at, pages_visited, links_found, dept_pages_found,
                     go_listings_found, pdf_links_found, downloaded_count, parsed_count,
                     ocr_count, failures_count, error_message, avg_response_time,
                     report_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id, run_data["started_at"], run_data["pages_visited"],
                    run_data["links_seen"], run_data["dept_pages_found"],
                    run_data["go_listings_found"], run_data["pdf_links_found"],
                    run_data["downloaded_count"], run_data["parsed_count"],
                    run_data["ocr_count"], run_data["parser_failures"] + run_data["download_failures"],
                    run_data["error"], avg_resp, report_text, utcnow()
                )
            )
            report_id = cur.lastrowid
            return RedirectResponse(f"/ops/reports/{report_id}", status_code=303)
        finally:
            fetcher.close()

    @app.get("/ops/reports/{report_id}", response_class=HTMLResponse)
    def report_detail(request: Request, report_id: int, conn: Conn, current_user: LoggedIn):
        report = conn.execute(
            """
            SELECT r.*, s.name AS source_name
              FROM diagnostic_reports r
              JOIN sources s ON s.id = r.source_id
             WHERE r.id = ?
            """,
            (report_id,),
        ).fetchone()
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return templates.TemplateResponse(
            request, "report_detail.html",
            {
                "report": report,
                "current_user": current_user,
            }
        )

    @app.get("/ops/reports/{report_id}/download")
    def report_download(report_id: int, conn: Conn, current_user: LoggedIn):
        report = conn.execute("SELECT report_text, source_id FROM diagnostic_reports WHERE id = ?", (report_id,)).fetchone()
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        
        source_name = conn.execute("SELECT name FROM sources WHERE id = ?", (report["source_id"],)).fetchone()["name"]
        filename = f"diagnostic_report_{source_name.lower().replace(' ', '_')}_{report_id}.txt"
        
        from fastapi.responses import Response as FastApiResponse
        return FastApiResponse(
            content=report["report_text"],
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    @app.get("/ops/crawl-runs/{run_id}/evidence", response_class=HTMLResponse)
    def run_evidence(request: Request, run_id: int, conn: Conn, current_user: LoggedIn):
        run = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="Crawl run not found")
        source_name = conn.execute("SELECT name FROM sources WHERE id = ?", (run["source_id"],)).fetchone()["name"]
        evidences = conn.execute("SELECT * FROM crawl_evidences WHERE crawl_run_id = ? ORDER BY id", (run_id,)).fetchall()
        return templates.TemplateResponse(
            request, "crawl_evidence.html",
            {
                "run": run,
                "source_name": source_name,
                "evidences": evidences,
                "current_user": current_user,
            }
        )

    @app.get("/ops/diagnostics/root-cause", response_class=HTMLResponse)
    def root_cause_dashboard(request: Request, conn: Conn, current_user: LoggedIn):
        stats_today = _get_failure_stats(conn, 0)
        stats_7days = _get_failure_stats(conn, 7)
        stats_30days = _get_failure_stats(conn, 30)

        # Breakdown across the actual Network Truth Framework categories --
        # not filtered to one literal category name, since every row's
        # failure_category is already one of the fourteen definitive causes.
        subtype_breakdown = conn.execute(
            """
            SELECT failure_category AS failure_subtype, COUNT(*) AS n
              FROM crawl_evidences
             WHERE failure_category IS NOT NULL AND failure_category != ''
             GROUP BY failure_category
             ORDER BY n DESC
            """
        ).fetchall()

        return templates.TemplateResponse(
            request, "root_cause.html",
            {
                "today": stats_today,
                "last_7_days": stats_7days,
                "last_30_days": stats_30days,
                "subtypes": subtype_breakdown,
                "current_user": current_user,
            }
        )

    @app.get("/ops/diagnostics/network", response_class=HTMLResponse)
    def network_diagnostics_hub(request: Request, conn: Conn, current_user: LoggedIn):
        stats = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS successful,
                   SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                   AVG(response_time_ms) AS avg_time
              FROM network_connectivity_tests
            """
        ).fetchone()

        common_failure = conn.execute(
            """
            SELECT failure_category, COUNT(*) AS n
              FROM network_connectivity_tests
             WHERE status = 'FAILED'
             GROUP BY failure_category
             ORDER BY n DESC LIMIT 1
            """
        ).fetchone()
        most_common_failure = common_failure["failure_category"] if common_failure else "None"

        latest_targets = conn.execute(
            """
            SELECT n1.*
              FROM network_connectivity_tests n1
              JOIN (
                  SELECT target_name, MAX(id) AS max_id
                    FROM network_connectivity_tests
                   GROUP BY target_name
              ) n2 ON n1.id = n2.max_id
             ORDER BY n1.target_name
            """
        ).fetchall()

        history = conn.execute(
            """
            SELECT * FROM network_connectivity_tests ORDER BY id DESC LIMIT 50
            """
        ).fetchall()

        from ..operations import network_diagnostics as ops_net_diag
        recent_runs = ops_net_diag.list_diagnostic_runs(conn, limit=5)
        active_run = next((r for r in recent_runs if r["status"] in ("QUEUED", "RUNNING")), None)

        return templates.TemplateResponse(
            request, "network_diagnostics.html",
            {
                "stats": stats,
                "most_common_failure": most_common_failure,
                "latest_targets": latest_targets,
                "history": history,
                "recent_runs": recent_runs,
                "active_run": active_run,
                "current_user": current_user,
            }
        )

    @app.post("/ops/diagnostics/network/run")
    def run_network_diagnostics(
        conn: Conn, current_user: LoggedIn, config: Config, fetcher_factory: FetcherFactory
    ):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        from ..operations import network_diagnostics as ops_net_diag
        run_id = ops_net_diag.start_diagnostic_run(
            config, created_by=current_user.username, fetcher_factory=fetcher_factory,
        )
        return RedirectResponse(f"/ops/diagnostics/network/runs/{run_id}", status_code=303)

    @app.get("/ops/diagnostics/network/runs/{run_id}", response_class=HTMLResponse)
    def network_diagnostic_run_detail(request: Request, run_id: int, conn: Conn, current_user: LoggedIn):
        from ..operations import network_diagnostics as ops_net_diag
        run = ops_net_diag.get_diagnostic_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Diagnostic run not found")

        results = []
        if run["started_at"]:
            end_time = run["finished_at"] or utcnow()
            results = conn.execute(
                """
                SELECT * FROM network_connectivity_tests
                 WHERE timestamp >= ? AND timestamp <= ?
                 ORDER BY id
                """,
                (run["started_at"], end_time),
            ).fetchall()

        return templates.TemplateResponse(
            request, "network_diagnostic_run.html",
            {"run": run, "results": results, "current_user": current_user},
        )

    @app.get("/ops/diagnostics/network/{test_id}", response_class=HTMLResponse)
    def network_test_detail(request: Request, test_id: int, conn: Conn, current_user: LoggedIn):
        import json
        test = conn.execute("SELECT * FROM network_connectivity_tests WHERE id = ?", (test_id,)).fetchone()
        if not test:
            raise HTTPException(status_code=404, detail="Test result not found")
        
        headers = {}
        if test["response_headers"]:
            try:
                headers = json.loads(test["response_headers"])
            except Exception:
                pass

        return templates.TemplateResponse(
            request, "network_test_detail.html",
            {
                "test": test,
                "headers": headers,
                "current_user": current_user,
            }
        )

    @app.get("/ops/diagnostics/network/{test_id}/download")
    def network_test_download(test_id: int, conn: Conn, current_user: LoggedIn):
        test = conn.execute("SELECT response_html, target_name FROM network_connectivity_tests WHERE id = ?", (test_id,)).fetchone()
        if not test or not test["response_html"]:
            raise HTTPException(status_code=404, detail="Content not available")
        
        filename = f"network_test_{test['target_name'].lower().replace(' ', '_')}_{test_id}.html"
        from fastapi.responses import Response as FastApiResponse
        return FastApiResponse(
            content=test["response_html"],
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


def _get_failure_stats(conn: sqlite3.Connection, days_ago: int) -> dict:
    from datetime import datetime, timezone, timedelta
    if days_ago == 0:
        start_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat(timespec="seconds")
    else:
        start_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")

    # Every one of the fourteen Network Truth Framework categories represents
    # a network/HTTP-layer failure (that is the whole domain this
    # classification covers) -- any non-empty failure_category counts here,
    # not a literal-string match against one specific category name.
    net_failures = conn.execute(
        "SELECT COUNT(*) FROM crawl_evidences WHERE failure_category IS NOT NULL "
        "AND failure_category != '' AND timestamp >= ?",
        (start_date,)
    ).fetchone()[0]

    disc_failures = conn.execute(
        "SELECT COUNT(*) FROM crawl_runs WHERE status = 'error' AND started_at >= ?",
        (start_date,)
    ).fetchone()[0]

    down_failures = conn.execute(
        "SELECT SUM(download_failures) FROM crawl_runs WHERE started_at >= ?",
        (start_date,)
    ).fetchone()[0] or 0

    parse_failures = conn.execute(
        "SELECT SUM(parser_failures) FROM crawl_runs WHERE started_at >= ?",
        (start_date,)
    ).fetchone()[0] or 0

    ocr_failures = conn.execute(
        "SELECT COUNT(*) FROM extraction_failures WHERE failure_type = 'ocr_failure' AND created_at >= ?",
        (start_date,)
    ).fetchone()[0]

    pub_failures = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'publication.failed' AND ts >= ?",
        (start_date,)
    ).fetchone()[0]

    return {
        "network": net_failures,
        "discovery": disc_failures,
        "download": down_failures,
        "parser": parse_failures,
        "ocr": ocr_failures,
        "publication": pub_failures,
    }


# ---------------------------------------------------------------------------
# Backup export + production reset. Reset never touches what the schema
# treats as permanent (documents/document_blobs, audit_log, the golden
# dataset, source_versions) -- see operations/reset.py's module docstring.
# ---------------------------------------------------------------------------
def _register_system_reset(app: FastAPI) -> None:
    @app.get("/ops/backup/download")
    def download_backup(conn: Conn, current_user: LoggedIn):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        snapshot = ops_backup.export_review_snapshot(conn)
        filename = f"thirdeye-review-backup-{utcnow().replace(':', '-')}.json"
        return Response(
            content=json.dumps(snapshot, indent=2, ensure_ascii=False, default=str),
            media_type="application/json",
            headers={"content-disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/ops/system-reset", response_class=HTMLResponse)
    def system_reset_page(request: Request, conn: Conn, current_user: LoggedIn):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        pending = conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"]
        return templates.TemplateResponse(
            request, "system_reset.html",
            {
                "current_user": current_user,
                "go_records_count": pending,
                "reset_result": None,
            },
        )

    @app.post("/ops/system-reset/run")
    def run_system_reset(
        request: Request, conn: Conn, current_user: LoggedIn, confirm: Annotated[str, Form()],
    ):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if confirm != "RESET":
            raise HTTPException(status_code=400, detail='confirmation phrase must be exactly "RESET"')
        result = ops_reset.reset_for_production(conn, actor=current_user.username)
        return templates.TemplateResponse(
            request, "system_reset.html",
            {
                "current_user": current_user,
                "go_records_count": 0,
                "reset_result": result,
            },
        )


# ---------------------------------------------------------------------------
# Notifications settings (EmailJS credentials + a test send). Only the send
# primitive and its credential storage live here -- deciding when/to whom an
# actual citizen-facing email (a digest, an alert) gets sent is a separate,
# not-yet-scoped feature.
# ---------------------------------------------------------------------------
def _register_notifications(app: FastAPI) -> None:
    @app.get("/ops/notifications", response_class=HTMLResponse)
    def notifications_hub(request: Request, conn: Conn, current_user: LoggedIn):
        cfg = ops_email.get_config(conn)
        return templates.TemplateResponse(
            request, "notifications.html",
            {
                "current_user": current_user,
                "service_id": cfg.get("emailjs_service_id", ""),
                "template_id": cfg.get("emailjs_template_id", ""),
                "public_key": cfg.get("emailjs_public_key", ""),
                "private_key": cfg.get("emailjs_private_key", ""),
                "is_configured": ops_email.is_configured(conn),
                "test_result": None,
            },
        )

    @app.post("/ops/notifications/emailjs")
    def notifications_save(
        conn: Conn, current_user: LoggedIn,
        service_id: Annotated[str, Form()], template_id: Annotated[str, Form()],
        public_key: Annotated[str, Form()], private_key: Annotated[str, Form()] = "",
    ):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        ops_email.save_config(
            conn, service_id=service_id, template_id=template_id,
            public_key=public_key, private_key=private_key,
        )
        return RedirectResponse("/ops/notifications", status_code=303)

    @app.post("/ops/notifications/test-email", response_class=HTMLResponse)
    def notifications_test(request: Request, conn: Conn, current_user: LoggedIn, to_email: Annotated[str, Form()]):
        cfg = ops_email.get_config(conn)
        try:
            ok, message = ops_email.send_email(
                conn, to_email=to_email,
                template_params={
                    "subject": "Third Eye — Test Notification",
                    "message": "This is a test email from Third Eye's Notifications settings. "
                               "If you received this, EmailJS is configured correctly.",
                },
            )
        except ops_email.EmailNotConfigured as exc:
            ok, message = False, str(exc)
        return templates.TemplateResponse(
            request, "notifications.html",
            {
                "current_user": current_user,
                "service_id": cfg.get("emailjs_service_id", ""),
                "template_id": cfg.get("emailjs_template_id", ""),
                "public_key": cfg.get("emailjs_public_key", ""),
                "private_key": cfg.get("emailjs_private_key", ""),
                "is_configured": ops_email.is_configured(conn),
                "test_result": {"ok": ok, "message": message},
            },
        )

