"""Phase 3.7 Initiative 4 -- Publication Validation.

One consolidated walk-through proving each blueprint checklist item in
sequence, using real (if synthetic) data end to end: a document is
extracted, sits in the Review Queue, goes through the Approval Workflow,
becomes visible via the Publication Workflow, is reachable in Repository
Visibility (the public /orders list and detail page), and resolves via its
Permanent URL (/go/{slug}). Each blueprint item names the assertion that
proves it, rather than leaving the mapping implicit."""

from __future__ import annotations

from goengine import review
from goengine.pipeline import run_all
from fastapi.testclient import TestClient

from goengine.workbench.app import create_app


def test_publication_validation_full_walkthrough(conn, settings, fetcher, source_id):
    client = TestClient(create_app(settings))

    # Extraction: sampledata's 3 GOs parse into pending go_records.
    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])

    # --- Review Queue --------------------------------------------------
    # A freshly-extracted record must be pending and appear in the queue.
    assert review.counts_by_status(conn)["pending"] >= 1
    summary = review.get_summary(conn, record_id)
    assert summary.status == "pending"

    # A pending record must not be publicly visible or fetchable yet.
    assert client.get("/orders").status_code == 200
    assert f"/orders/{record_id}" not in client.get("/orders").text
    assert client.get(f"/orders/{record_id}").status_code == 404

    # --- Approval Workflow ----------------------------------------------
    review.approve(conn, record_id, reviewer="phase37-validator")
    approved_summary = review.get_summary(conn, record_id)
    assert approved_summary.status == "approved"
    assert approved_summary.reviewed_by == "phase37-validator"
    assert approved_summary.reviewed_at is not None

    # --- Publication Workflow -------------------------------------------
    # Approval alone is what makes a record part of the Verified GO Database.
    published = review.verified_records(conn)
    assert any(r["record_id"] == record_id for r in published)

    # --- Repository Visibility -------------------------------------------
    listing = client.get("/orders")
    assert listing.status_code == 200
    detail = client.get(f"/orders/{record_id}")
    assert detail.status_code == 200
    assert "Verified" in detail.text

    # --- Permanent URLs ---------------------------------------------------
    slug_row = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert slug_row["go_url_slug"] is not None
    permanent = client.get(f"/go/{slug_row['go_url_slug']}")
    assert permanent.status_code == 200
    assert permanent.text == detail.text  # identical rendering via either URL


def test_publication_validation_rejection_keeps_record_unpublished(conn, settings, fetcher, source_id):
    """The Approval Workflow's counterpart: a rejected record must never
    become visible, satisfying "Publication Workflow" and "Repository
    Visibility" for the negative case too, not just the happy path."""
    client = TestClient(create_app(settings))
    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])

    review.reject(conn, record_id, reviewer="phase37-validator", reason="Phase 3.7 validation")

    assert not any(r["record_id"] == record_id for r in review.verified_records(conn))
    assert client.get(f"/orders/{record_id}").status_code == 404
    assert f"/orders/{record_id}" not in client.get("/orders").text
