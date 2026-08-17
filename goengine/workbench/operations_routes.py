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
    ):
        if state_id is not None and not current_user.can_act_on_state(state_id):
            raise HTTPException(status_code=403, detail="not authorized for this state")
        try:
            source_id = ops_sources.create_source(
                conn, name=name, department=department, url=url, source_type=source_type,
                discovery_method=discovery_method or None, state_id=state_id, district_id=district_id,
                priority=priority, source_category=source_category or None,
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
        return templates.TemplateResponse(request, "job_detail.html", {"job": job, "current_user": current_user})

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
