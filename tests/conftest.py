from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from goengine import pipeline, registry
from goengine.config import Settings
from goengine.db import init_db
from goengine.fetching import OfflineFetcher
from goengine.operations import auth as ops_auth
from goengine.sampledata import SAMPLES, write_samples

LISTING_URL = "https://cms.tn.gov.in/go-search"
FILE_BASE = "https://cms.tn.gov.in/sites/default/files/go"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    resolved = Settings.load(tmp_path / "data")
    resolved.ensure_dirs()
    return resolved


@pytest.fixture
def conn(settings: Settings):
    connection = init_db(settings)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def source_id(conn: sqlite3.Connection) -> int:
    return registry.add_source(
        conn,
        name="Tamil Nadu GO Portal",
        department="All Departments",
        url=LISTING_URL,
        source_type="go_portal",
        adapter="tn_go_portal",
    )


@pytest.fixture
def sample_pdfs(tmp_path: Path):
    return write_samples(tmp_path / "samples")


def build_listing_html(written) -> str:
    rows = "\n".join(
        f'<tr><td>{sample.go_number}</td><td>{sample.printed_date}</td>'
        f'<td>{sample.department}</td>'
        f'<td><a href="{FILE_BASE}/{path.name}">{sample.go_number} abstract</a></td></tr>'
        for sample, path in written
    )
    return (
        "<html><body><table>"
        "<tr><th>G.O. No</th><th>Date</th><th>Department</th><th>Subject</th></tr>"
        f"{rows}</table></body></html>"
    )


@pytest.fixture
def fetcher(sample_pdfs) -> OfflineFetcher:
    offline = OfflineFetcher()
    offline.add_html(LISTING_URL, build_listing_html(sample_pdfs))
    for _, path in sample_pdfs:
        offline.add_bytes(f"{FILE_BASE}/{path.name}", path.read_bytes())
    return offline


@pytest.fixture
def samples():
    return SAMPLES


def login_as(
    test_client,
    conn: sqlite3.Connection,
    *,
    username: str = "admin",
    password: str = "testpass123",
    role: str = ops_auth.ROLE_PLATFORM_ADMIN,
    state_id: int | None = None,
) -> str:
    """Bootstrap a user directly in the DB and authenticate `test_client`
    against it (TestClient persists the session cookie for later requests).
    Every Phase 3 workbench route requires a session -- HTTP tests need this
    before hitting anything but /login or /setup."""
    ops_auth.create_user(
        conn, username=username, password=password, role=role, state_id=state_id,
        actor="test-bootstrap",
    )
    response = test_client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 200, f"login failed: {response.status_code} {response.text[:200]}"
    return username


@pytest.fixture
def parsed_documents(conn: sqlite3.Connection, settings: Settings, fetcher: OfflineFetcher, source_id: int) -> list[int]:
    """Run the full Phase 1 pipeline end to end. Returns document ids in the
    same order as `samples`/`sample_pdfs`, ready for Phase 2 certification
    fixtures to build golden documents and categories on top of."""
    pipeline.run_all(conn, settings, fetcher, only_due=False)
    rows = conn.execute("SELECT id FROM documents ORDER BY id").fetchall()
    return [int(r["id"]) for r in rows]
