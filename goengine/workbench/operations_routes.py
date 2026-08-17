"""Phase 3 routes: states, districts, departments, versioned sources,
certification jobs, publication, users, system health.

Split out of app.py to keep that module from growing without bound as
Phase 3's admin surface expands -- registered via `register(app, templates)`
from `create_app`. Uses the same Conn/LoggedIn/RequireX dependency pattern
established there (see deps.py).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import audit, registry, repository
from ..db import utcnow
from ..certification.categorize import ALL_BUCKETS
from ..certification.sources import certification_history
from ..operations import auth
from ..operations import departments as ops_departments
from ..operations import geography
from ..operations import dashboard as ops_dashboard
from ..operations import health as ops_health
from ..operations import jobs as ops_jobs
from ..operations import publication as ops_publication
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
    _register_health(app)
    _register_users(app)
    _register_diagnostics(app)


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
    def districts_list(request: Request, conn: Conn, current_user: LoggedIn, state_id: int | None = None):
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
    @app.get("/ops/jobs", response_class=HTMLResponse)
    def jobs_list(request: Request, conn: Conn, current_user: LoggedIn):
        return templates.TemplateResponse(
            request, "jobs.html",
            {
                "jobs": ops_jobs.list_jobs(conn),
                "states": geography.list_states(conn),
                "current_user": current_user,
                "can_run": current_user.has_permission("run_certification"),
                "buckets": ALL_BUCKETS,
            },
        )

    @app.post("/ops/jobs/start")
    def jobs_start(
        request: Request, conn: Conn, config: Config, current_user: RequireCertify,
        fetcher_factory: FetcherFactory,
        state_id: Annotated[int | None, Form()] = None,
        district_id: Annotated[int | None, Form()] = None,
        departments: Annotated[list[str] | None, Form()] = None,
    ):
        if state_id is not None and not current_user.can_act_on_state(state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        job_id = ops_jobs.start_job(
            config, state_id=state_id, district_id=district_id,
            department_filter=departments or None, created_by=current_user.username,
            fetcher_factory=fetcher_factory,
        )
        return RedirectResponse(f"/ops/jobs/{job_id}", status_code=303)

    @app.get("/ops/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int, conn: Conn, current_user: LoggedIn):
        job = ops_jobs.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        
        runs = []
        if job["started_at"]:
            end_time = job["finished_at"] or utcnow()
            runs = conn.execute(
                """
                SELECT c.*, s.name AS source_name
                  FROM crawl_runs c
                  JOIN sources s ON s.id = c.source_id
                 WHERE c.started_at >= ? AND c.started_at <= ?
                 ORDER BY c.id
                """,
                (job["started_at"], end_time)
            ).fetchall()

        return templates.TemplateResponse(
            request, "job_detail.html",
            {
                "job": job,
                "runs": runs,
                "current_user": current_user,
            }
        )

    @app.get("/api/jobs/{job_id}")
    def job_status_api(job_id: int, conn: Conn, current_user: LoggedIn):
        job = ops_jobs.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return JSONResponse(dict(job))


# ---------------------------------------------------------------------------
# Document Library -- every downloaded file, independent of review status,
# with a download link. Composition only: reads through repository.py and
# the same tables the review workbench and dashboard already use.
# ---------------------------------------------------------------------------
def _register_documents(app: FastAPI) -> None:
    @app.get("/ops/documents", response_class=HTMLResponse)
    def documents_list(
        request: Request, conn: Conn, current_user: LoggedIn,
        source_id: int | None = None, q: str | None = None, department: str | None = None,
        year: int | None = None, language: str | None = None, status: str | None = None,
    ):
        return templates.TemplateResponse(
            request, "documents.html",
            {
                "documents": repository.list_documents(
                    conn, source_id=source_id, search=q, department=department,
                    year=year, language=language, status=status,
                ),
                "sources": registry.list_sources(conn),
                "departments": repository.list_document_departments(conn),
                "years": repository.list_document_years(conn),
                "languages": ("english", "tamil", "mixed", "unknown"),
                "statuses": ("approved", "rejected", "pending", "new", "downloaded", "parsed", "verified"),
                "selected_source_id": source_id,
                "search": q or "",
                "selected_department": department or "",
                "selected_year": year,
                "selected_language": language or "",
                "selected_status": status or "",
                "current_user": current_user,
            },
        )


# ---------------------------------------------------------------------------
# Module 7: Review Workbench (typed queues + escalation)
# ---------------------------------------------------------------------------
def _register_review(app: FastAPI) -> None:
    @app.get("/ops/review", response_class=HTMLResponse)
    def review_hub(request: Request, conn: Conn, current_user: LoggedIn, queue: str = ops_review.QUEUE_EXTRACTION):
        if queue not in (ops_review.QUEUE_EXTRACTION, ops_review.QUEUE_OCR, ops_review.QUEUE_METADATA, ops_review.QUEUE_FAILURE):
            raise HTTPException(status_code=400, detail="unknown queue type")
        return templates.TemplateResponse(
            request, "review_hub.html",
            {
                "counts": ops_review.queue_counts(conn),
                "selected_queue": queue,
                "records": ops_review.queue_by_type(conn, queue, limit=50),
                "open_escalations": ops_review.open_escalations(conn),
                "current_user": current_user,
                "can_escalate": current_user.has_permission("escalate_records"),
            },
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
                report_lines.append(
                    f"URL: {ev['url']}\n"
                    f"  Status: {ev['status_code']}\n"
                    f"  Size: {ev['response_size']} bytes\n"
                    f"  Duration: {ev['duration_ms']:.1f}ms\n"
                    f"  Redirects: {ev['redirect_count']}\n"
                    f"  SSL Verified: {ev['ssl_verified']}\n"
                    f"  Category: {ev['failure_category'] or 'None'}\n"
                    f"  Subtype: {ev['failure_subtype'] or 'None'}\n"
                    f"  Error: {ev['error_message'] or 'None'}\n"
                )
            
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

        subtype_breakdown = conn.execute(
            """
            SELECT failure_subtype, COUNT(*) AS n
              FROM crawl_evidences
             WHERE failure_category = 'network_failure'
             GROUP BY failure_subtype
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

        return templates.TemplateResponse(
            request, "network_diagnostics.html",
            {
                "stats": stats,
                "most_common_failure": most_common_failure,
                "latest_targets": latest_targets,
                "history": history,
                "current_user": current_user,
            }
        )

    @app.post("/ops/diagnostics/network/run")
    def run_network_diagnostics(conn: Conn, current_user: LoggedIn, fetcher_factory: FetcherFactory):
        if not current_user.has_permission("manage_sources"):
            raise HTTPException(status_code=403, detail="Not authorized")
        from ..operations import network_diagnostics as ops_net_diag
        fetcher = fetcher_factory()
        try:
            ops_net_diag.run_all_diagnostics(conn, fetcher)
        finally:
            fetcher.close()
        return RedirectResponse("/ops/diagnostics/network", status_code=303)

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

    net_failures = conn.execute(
        "SELECT COUNT(*) FROM crawl_evidences WHERE failure_category = 'network_failure' AND timestamp >= ?",
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

