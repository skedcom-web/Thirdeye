"""cli.py::cmd_agent_daemon -- the --idle-exit-after auto-stop behavior, and
(Phase 3.7 Initiative 6) the full agent lifecycle: starts on a request,
processes it, completes it, and exits automatically once the queue is
empty -- no orphan process, since it's all one function call on one thread.

--daemon exits on its own once the queue has been empty for a few
consecutive polls, instead of looping forever until Ctrl+C (see
cmd_agent_daemon's own docstring for why "launch a process on the admin's
separate machine" isn't achievable here -- this is the part that is).

Also covers a real production performance bug found and fixed after a live
run: _run_claimed_request used to re-run full page discovery on every
single download/parse batch (pipeline.run_all() inside the batch loop),
re-crawling the same listing pages dozens of times over for a large
department and paying the 1.5s per-host politeness delay each time for
pages that had already been fully seen. Discovery now runs exactly once per
source, before the batch loop."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from goengine import cli, pipeline


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHttpxClient:
    """Always reports an empty queue -- exercises the idle-exit path
    without a real server."""

    def __init__(self, *args, **kwargs):
        self.post_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url: str, headers=None, json=None):
        self.post_calls += 1
        return _FakeResponse({"request": None})


@pytest.fixture
def args(settings, tmp_path):
    return argparse.Namespace(
        data_dir=settings.data_dir,
        server_url="https://example.invalid",
        api_key="test-key",
        poll_interval=0,
        daemon=True,
        idle_exit_after=3,
    )


def test_daemon_exits_after_idle_exit_after_consecutive_empty_polls(monkeypatch, args):
    import httpx
    import time

    fake_client = _FakeHttpxClient()
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake_client)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    exit_code = cli.cmd_agent_daemon(args)

    assert exit_code == 0
    assert fake_client.post_calls == args.idle_exit_after


class _FakeLifecycleHttpxClient:
    """Simulates exactly one real request already queued, then an empty
    queue -- exercises the FULL agent lifecycle (claim -> mirror sources ->
    attempt processing -> report completion via the API -> idle-exit) in
    one continuous run, per Phase 3.7 Initiative 6. No sources are mirrored
    from the (fake) server, so processing legitimately fails with the
    already-existing "no local sources match this request's scope" error --
    a real code path, not a network call -- which still proves the agent
    completes its work cycle and reports back rather than hanging, and then
    exits on its own once there's nothing left to do."""

    def __init__(self):
        self.claim_calls = 0
        self.complete_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url: str, headers=None):
        if url.endswith("/api/agent/sources"):
            return _FakeResponse([])
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, headers=None, json=None):
        if url.endswith("/queue/claim"):
            self.claim_calls += 1
            if self.claim_calls == 1:
                return _FakeResponse({
                    "request": {
                        "id": 1, "kind": "extraction", "state_name": None,
                        "district_name": None, "department_filter": None,
                    }
                })
            return _FakeResponse({"request": None})
        if url.endswith("/complete"):
            self.complete_calls.append(json)
            return _FakeResponse({"ok": True})
        if url.endswith("/progress"):
            return _FakeResponse({"ok": True})
        raise AssertionError(f"unexpected POST {url}")


def test_full_lifecycle_claim_process_complete_then_idle_exit(monkeypatch, args):
    """Proves "starts -> processes -> completes -> exits automatically" as
    one sequence -- the daemon claims a real queued request, works it
    (reporting completion either way via the API), then auto-exits once the
    queue is empty. `cmd_agent_daemon` returning normally (no thread/process
    left behind) is itself the proof there's no orphan process -- everything
    here runs in this one function call, on this one thread."""
    import httpx
    import time

    fake_client = _FakeLifecycleHttpxClient()
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake_client)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    exit_code = cli.cmd_agent_daemon(args)

    assert exit_code == 0
    assert fake_client.claim_calls == 1 + args.idle_exit_after  # 1 real claim + N idle polls to exit
    assert len(fake_client.complete_calls) == 1
    assert fake_client.complete_calls[0]["ok"] is False
    assert "no local sources match" in fake_client.complete_calls[0]["error"]


def test_idle_exit_after_zero_never_auto_exits(monkeypatch, args):
    """Regression guard for the opt-out: idle_exit_after=0 must behave like
    the old forever-loop, stopping only via the SIGINT flag -- simulated
    here by flipping it after a fixed number of polls."""
    import httpx
    import time

    args.idle_exit_after = 0
    fake_client = _FakeHttpxClient()
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake_client)

    def fake_sleep(_seconds):
        if fake_client.post_calls >= 5:
            raise KeyboardInterrupt  # stand-in for Ctrl+C, since there's no idle-exit to rely on

    monkeypatch.setattr(time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_agent_daemon(args)

    assert fake_client.post_calls >= 5


# ---------------------------------------------------------------------------
# Performance regression: discovery must run once per source, not once per
# download/parse batch.
# ---------------------------------------------------------------------------
class _FakeSyncResponse:
    """Stands in for the httpx.Response _sync_documents expects when
    uploading a downloaded document back to the (fake) server."""

    status_code = 200

    def json(self):
        return {"already_synced": False, "go_record_id": 1}

    @property
    def text(self):
        return ""


class _FakeRealWorkHttpxClient:
    """Accepts the progress/sync/complete POSTs _run_claimed_request makes
    while doing real (offline-fetched) discovery/download/parse work, so the
    actual batch loop runs end to end against real sample data."""

    def post(self, url: str, headers=None, json=None, files=None, data=None):
        if url.endswith("/sync/document"):
            return _FakeSyncResponse()
        return _FakeResponse({"ok": True})


def test_discovery_runs_once_per_source_not_once_per_batch(conn, settings, fetcher, source_id, monkeypatch):
    """The actual bug, reproduced directly: with _BATCH_SIZE=10 and only 3
    sample documents, the old code (pipeline.run_all() inside the batch
    loop) called discovery twice -- once to find and download all 3 in the
    first batch, once more to confirm the source was exhausted. The fixed
    code calls it exactly once, before the batch loop starts, regardless of
    how many download/parse batches follow."""
    discovery_calls = []
    original_run_discovery = pipeline.run_discovery

    def counting_run_discovery(*args, **kwargs):
        discovery_calls.append(kwargs.get("source_id", (args[2] if len(args) > 2 else None)))
        return original_run_discovery(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_discovery", counting_run_discovery)

    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }
    cli._run_claimed_request(
        conn, settings, fetcher, _FakeRealWorkHttpxClient(), "https://example.invalid", {}, req,
    )

    assert len(discovery_calls) == 1
    assert discovery_calls[0] == source_id

    # And the real work still happened: sampledata's 3 documents were
    # actually discovered, downloaded, and parsed via this one pass.
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3
