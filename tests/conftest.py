from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from goengine import registry
from goengine.config import Settings
from goengine.db import init_db
from goengine.fetching import OfflineFetcher
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
