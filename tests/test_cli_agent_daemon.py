"""cli.py::cmd_agent_daemon -- the --idle-exit-after auto-stop behavior, and
(Phase 3.7 Initiative 6) the full agent lifecycle: starts on a request,
processes it, completes it, and exits automatically once the queue is
empty -- no orphan process, since it's all one function call on one thread.

--daemon exits on its own once the queue has been empty for a few
consecutive polls, instead of looping forever until Ctrl+C (see
cmd_agent_daemon's own docstring for why "launch a process on the admin's
separate machine" isn't achievable here -- this is the part that is).

Also covers real production reliability bugs found and fixed after live
runs:

1. _run_claimed_request used to re-run full page discovery on every single
   download/parse batch (pipeline.run_all() inside the batch loop),
   re-crawling the same listing pages dozens of times over for a large
   department and paying the 1.5s per-host politeness delay each time for
   pages that had already been fully seen. Discovery now runs exactly once
   per source, before the batch loop.

2. Progress was only reported to the server once a whole _BATCH_SIZE=10-wide
   parse batch finished -- parsing includes OCR, and a single heavily-scanned
   multi-page GO can take several minutes by itself, so the dashboard could
   sit at 0 parsed for 15+ minutes of real, ongoing work (confirmed live: a
   24-page scanned document took ~6 minutes alone). Parsing is now done and
   reported one document at a time (_PARSE_BATCH_SIZE=1).

3. A single unhandled exception ANYWHERE in _run_claimed_request (a broken
   government host for one of several departments, a transient network blip
   on a progress POST) aborted the ENTIRE request via cmd_agent_daemon's
   broad `except Exception`, reporting it FAILED regardless of how many
   hours of real, already-synced work came before it -- confirmed live: a
   10-department request ran 16+ hours, genuinely downloaded and parsed 80
   documents, then came back FAILED because of a single error on one
   source. Each source's work is now isolated (one source's exception is
   recorded and skipped, not fatal to the others) and progress POSTs retry
   transient failures instead of raising immediately; the request is only
   reported failed if literally nothing worked anywhere.

4. Even with fix #3, a single source with an unexpectedly large real
   backlog could occupy _run_claimed_request (and therefore the whole
   single-threaded daemon) forever without ever raising an exception --
   confirmed live: one department (Water Resources) ran continuously for
   16.8 hours without exhausting, during which every OTHER queued request
   sat untouched with zero chance of being claimed, no matter how small its
   own scope was. A wall-clock budget (_REQUEST_TIME_BUDGET_SECONDS) now
   caps the whole call; once hit, it returns early with honest partial
   progress already reported and synced, so the daemon goes back to polling
   and other requests get a fair turn. A follow-up request for the same
   scope resumes exactly where the last one left off.

5. Clicking Cancel on the dashboard only ever updated server-side
   bookkeeping -- there was no channel to tell a still-running agent to
   actually stop, so it kept working (and kept reporting progress) for
   however long its current source took, regardless of the click. Fixed
   using the channel that already exists: report_progress() now refuses to
   update an already-terminal (cancelled) request and returns its status;
   the /progress endpoint passes that status back in its response; and the
   agent checks it after every report (once per document) and raises
   RequestCancelled the moment it's no longer RUNNING/CLAIMED -- stopping
   within one report instead of running to natural completion or the time
   budget. Never recorded as a source error, and no pointless /complete
   call follows (the request is already in its final state)."""

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


def test_discovery_stops_once_exhausted_instead_of_repeating_per_batch(conn, settings, fetcher, source_id, monkeypatch):
    """The actual bug, reproduced directly: with _BATCH_SIZE=10 and only 3
    sample documents (all fit in one download/parse batch), the old code
    (pipeline.run_all() inside the batch loop) called discovery on every
    single batch indefinitely -- for a source with a large real backlog
    needing dozens of batches, that meant dozens of redundant re-crawls of
    the same listing pages. The fix calls discovery repeatedly too (small,
    bounded max_pages=5 each time, so progress keeps appearing), but stops
    once it has found nothing new twice in a row (_DISCOVERY_EXHAUSTED_AFTER)
    rather than continuing to call it on every remaining batch -- a small,
    bounded number of calls (3 here: one real pass, two confirmations),
    never proportional to how many download/parse batches a large backlog
    needs."""
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

    assert len(discovery_calls) == cli._DISCOVERY_EXHAUSTED_AFTER + 1
    assert all(sid == source_id for sid in discovery_calls)

    # And the real work still happened despite the extra confirmation passes.
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3

    # And the real work still happened: sampledata's 3 documents were
    # actually discovered, downloaded, and parsed via this one pass.
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3


class _CountingProgressHttpxClient:
    """Same real-work acceptance as _FakeRealWorkHttpxClient, but records
    every /progress POST's documents_parsed value so a test can see how
    granular progress reporting actually was during the run."""

    def __init__(self):
        self.progress_calls: list[dict] = []

    def post(self, url: str, headers=None, json=None, files=None, data=None):
        if url.endswith("/sync/document"):
            return _FakeSyncResponse()
        if url.endswith("/progress"):
            self.progress_calls.append(dict(json))
        return _FakeResponse({"ok": True})


def test_progress_reports_after_every_parsed_document_not_once_per_batch(conn, settings, fetcher, source_id):
    """The actual production incident, reproduced directly: parsing used to
    run as one _BATCH_SIZE=10-wide call, so the dashboard showed 0 parsed for
    however long the WHOLE batch's OCR took -- a batch of heavily-scanned
    documents could mean 15+ minutes of real progress with nothing visible.
    With sampledata's 3 documents, the fix must post progress once per
    parsed document (documents_parsed climbing 0->1->2->3), not jump straight
    from 0 to 3 in a single post."""
    client = _CountingProgressHttpxClient()
    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }
    cli._run_claimed_request(conn, settings, fetcher, client, "https://example.invalid", {}, req)

    parsed_progression = [c["documents_parsed"] for c in client.progress_calls if "documents_parsed" in c]
    # Every distinct step from 0 up to 3 must appear as its OWN post --
    # proof that a report went out after each individual parse, not once for
    # the whole batch of 3.
    assert 0 in parsed_progression
    assert 1 in parsed_progression
    assert 2 in parsed_progression
    assert 3 in parsed_progression


# ---------------------------------------------------------------------------
# Reliability: retry transient network blips, isolate per-source failures,
# and report completion honestly (partial success != total failure).
# ---------------------------------------------------------------------------
class _CompletionCapturingHttpxClient:
    """Real-work acceptance (like _FakeRealWorkHttpxClient) that also
    records every /complete and /progress POST's body, so a test can
    inspect exactly what was reported to the server -- including a
    yield_and_requeue progress call, which is how a request that hit its
    time budget reports itself instead of via /complete."""

    def __init__(self):
        self.complete_calls: list[dict] = []
        self.progress_calls: list[dict] = []

    def post(self, url: str, headers=None, json=None, files=None, data=None):
        if url.endswith("/sync/document"):
            return _FakeSyncResponse()
        if url.endswith("/complete"):
            self.complete_calls.append(dict(json))
        elif url.endswith("/progress"):
            self.progress_calls.append(dict(json) if json else {})
        return _FakeResponse({"ok": True})


def test_post_with_retry_succeeds_after_transient_failures(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    attempts = []

    class FlakyClient:
        def post(self, url, headers=None, json=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("transient network blip")
            return _FakeResponse({"ok": True})

    result = cli._post_with_retry(FlakyClient(), "https://example.invalid/x", headers={}, json={})
    assert result.json() == {"ok": True}
    assert len(attempts) == 3


def test_post_with_retry_raises_after_exhausting_attempts(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    class AlwaysFailingClient:
        def post(self, url, headers=None, json=None):
            raise ConnectionError("server unreachable")

    with pytest.raises(ConnectionError, match="server unreachable"):
        cli._post_with_retry(AlwaysFailingClient(), "https://example.invalid/x", headers={}, json={}, attempts=3)


def test_one_failing_source_does_not_abort_the_others(conn, settings, fetcher, source_id, monkeypatch):
    """The actual production incident, reproduced directly: two sources in
    scope, one of which fails outright during discovery. The other source's
    real work (sampledata's 3 documents) must still complete -- a broken
    department must not cost the rest of the request its progress."""
    from goengine import registry

    broken_source_id = registry.add_source(
        conn, name="Broken Department Source", department="Broken Department",
        url="https://cms.tn.gov.in/broken", source_type="go_portal",
    )

    original_run_discovery = pipeline.run_discovery

    def flaky_run_discovery(*args, **kwargs):
        if kwargs.get("source_id") == broken_source_id:
            raise ConnectionError("government host unreachable")
        return original_run_discovery(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_discovery", flaky_run_discovery)

    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }
    client = _CompletionCapturingHttpxClient()
    cli._run_claimed_request(conn, settings, fetcher, client, "https://example.invalid", {}, req)

    # The working source's real documents still landed despite the other
    # source's total failure.
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3

    assert len(client.complete_calls) == 1
    completion = client.complete_calls[0]
    assert completion["ok"] is True  # real progress happened -- not a total failure
    assert "Broken Department" not in completion.get("error", "") or str(broken_source_id) in completion.get("error", "")
    assert "government host unreachable" in completion.get("error", "")


def test_completion_reports_failure_only_when_nothing_at_all_worked(conn, settings, source_id, monkeypatch):
    """The inverse case: if the only source in scope fails completely with
    zero progress anywhere, the request must still be reported as failed --
    partial-success reporting must not paper over a run that accomplished
    nothing."""
    def always_raise(*args, **kwargs):
        raise ConnectionError("completely unreachable")

    monkeypatch.setattr(pipeline, "run_discovery", always_raise)

    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }
    client = _CompletionCapturingHttpxClient()
    cli._run_claimed_request(conn, settings, None, client, "https://example.invalid", {}, req)

    assert len(client.complete_calls) == 1
    completion = client.complete_calls[0]
    assert completion["ok"] is False
    assert "completely unreachable" in completion["error"]


# ---------------------------------------------------------------------------
# Fairness: a single source with a much larger real backlog than expected
# must not be able to occupy the whole claimed request (and therefore the
# whole single-threaded daemon) forever, starving every other queued
# request of any chance to be claimed.
# ---------------------------------------------------------------------------
def test_time_budget_yields_with_sources_left_for_a_followup_request(conn, settings, fetcher, source_id, monkeypatch):
    """The actual production incident, reproduced directly: confirmed live
    that one department (Water Resources) ran continuously for 16.8 hours
    without exhausting, during which the daemon could not even glance at
    other queued requests -- no matter how small their own scope was. Two
    sources in scope here; the fake clock jumps past the budget the instant
    the first source's backlog is fully drained, so the second is never
    started. This must NOT be reported via /complete (a second real
    incident: a 9-department request showed "COMPLETED" after only 1 was
    actually done) -- it goes back to the queue via a yield_and_requeue
    progress report instead, with its real progress intact, so the daemon's
    own poll loop naturally resumes it later alongside any other queued
    work."""
    import time as time_module

    from goengine import registry

    second_source_id = registry.add_source(
        conn, name="Second Department Source", department="Second Department",
        url="https://cms.tn.gov.in/second", source_type="go_portal",
    )
    monkeypatch.setattr(cli, "_REQUEST_TIME_BUDGET_SECONDS", 1000)

    real_monotonic = time_module.monotonic
    clock = {"offset": 0.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: real_monotonic() + clock["offset"])

    original_run_parsing = pipeline.run_parsing

    def parsing_that_exhausts_the_clock(*args, **kwargs):
        report = original_run_parsing(*args, **kwargs)
        if kwargs.get("source_id") == source_id and report.processed == 0:
            # source_id's backlog (sampledata's 3 documents) is fully
            # drained -- jump the clock past the budget so the SECOND
            # source is never even started.
            clock["offset"] = 100_000
        return report

    monkeypatch.setattr(pipeline, "run_parsing", parsing_that_exhausts_the_clock)

    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }
    client = _CompletionCapturingHttpxClient()
    cli._run_claimed_request(conn, settings, fetcher, client, "https://example.invalid", {}, req)

    # The first source's real work landed; the second was never touched.
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 3
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM documents WHERE source_id = ?", (second_source_id,)
    ).fetchone()["n"] == 0

    # No /complete call at all -- the request is not done, so nothing is
    # reported as either a success or a failure.
    assert client.complete_calls == []

    yield_calls = [c for c in client.progress_calls if c.get("yield_and_requeue")]
    assert len(yield_calls) == 1
    yielded = yield_calls[0]
    # documents_downloaded/parsed reflect the first source's real, landed
    # work -- not reset to 0 -- so a resumed slice's dashboard numbers don't
    # regress.
    assert yielded["documents_downloaded"] == 3
    assert yielded["documents_parsed"] == 3


# ---------------------------------------------------------------------------
# Cancellation: an admin's Cancel click must reach a running agent, not just
# update the dashboard while the agent grinds on for hours regardless.
# ---------------------------------------------------------------------------
class _CancellingHttpxClient:
    """Real-work acceptance (like _FakeRealWorkHttpxClient) whose /progress
    responses report the request as FAILED (as if an admin clicked Cancel
    server-side) after a configurable number of real progress reports --
    simulating a cancellation landing partway through a run."""

    def __init__(self, *, cancel_after_n_progress_calls: int):
        self.cancel_after = cancel_after_n_progress_calls
        self.progress_calls = 0
        self.complete_calls: list[dict] = []

    def post(self, url: str, headers=None, json=None, files=None, data=None):
        if url.endswith("/sync/document"):
            return _FakeSyncResponse()
        if url.endswith("/progress"):
            self.progress_calls += 1
            status = "FAILED" if self.progress_calls > self.cancel_after else "RUNNING"
            return _FakeResponse({"ok": True, "status": status})
        if url.endswith("/complete"):
            self.complete_calls.append(dict(json) if json else {})
        return _FakeResponse({"ok": True})


def test_agent_stops_within_one_progress_report_after_being_cancelled(conn, settings, fetcher, source_id):
    """The actual fix for "I clicked Cancel and it kept running": the agent
    now checks the /progress response after every report (one per document)
    and stops the moment it sees the request is no longer active -- instead
    of grinding on until its current source naturally exhausts or the time
    budget expires."""
    client = _CancellingHttpxClient(cancel_after_n_progress_calls=1)
    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }

    with pytest.raises(cli.RequestCancelled):
        cli._run_claimed_request(conn, settings, fetcher, client, "https://example.invalid", {}, req)

    # Stopped early -- not all 3 sample documents were parsed, proving this
    # didn't run to natural completion.
    parsed = conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"]
    assert parsed < 3
    # No completion call was ever made -- the request is already terminal
    # server-side (that's WHY the agent stopped), so there is nothing to
    # report; attempting one would be pointless at best.
    assert client.complete_calls == []


def test_cancellation_during_one_source_does_not_get_recorded_as_a_source_error(conn, settings, fetcher, source_id):
    """A cancellation must propagate straight out, not get swallowed by the
    per-source `except Exception` that isolates genuine per-source failures
    -- it is not a failure, and must not be retried or reported as one."""
    from goengine import registry

    registry.add_source(
        conn, name="Second Department Source", department="Second Department",
        url="https://cms.tn.gov.in/second", source_type="go_portal",
    )
    client = _CancellingHttpxClient(cancel_after_n_progress_calls=0)
    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }

    with pytest.raises(cli.RequestCancelled):
        cli._run_claimed_request(conn, settings, fetcher, client, "https://example.invalid", {}, req)


class _CancelledLifecycleHttpxClient:
    """Like _FakeLifecycleHttpxClient, but the one real claimed request gets
    cancelled immediately (its very first /progress call reports FAILED) --
    proves cmd_agent_daemon treats this as a clean stop, not a crash or a
    reported failure, and keeps polling afterward."""

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
            return _FakeResponse({"ok": True, "status": "FAILED"})
        raise AssertionError(f"unexpected POST {url}")


def test_cmd_agent_daemon_treats_cancellation_as_a_clean_stop_not_a_crash(monkeypatch, args):
    """cmd_agent_daemon's broad `except Exception` must not catch
    RequestCancelled and report it as a failure -- it's an admin's
    deliberate action, not something gone wrong, and posting /complete for
    an already-cancelled request would be pointless. The daemon must also
    keep polling afterward (proven here by reaching its normal idle-exit).
    _run_claimed_request itself is mocked to raise directly -- the mechanics
    of IT detecting a cancellation are already covered by
    test_agent_stops_within_one_progress_report_after_being_cancelled; this
    test is only about cmd_agent_daemon's own handling of that outcome."""
    import httpx
    import time

    fake_client = _CancelledLifecycleHttpxClient()
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake_client)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def raise_cancelled(*args, **kwargs):
        raise cli.RequestCancelled("request 1 is no longer active (status: FAILED)")

    monkeypatch.setattr(cli, "_run_claimed_request", raise_cancelled)

    exit_code = cli.cmd_agent_daemon(args)

    assert exit_code == 0
    assert fake_client.claim_calls == 1 + args.idle_exit_after
    assert fake_client.complete_calls == []  # no pointless /complete for an already-terminal request


def test_yielded_request_resumes_and_eventually_completes_with_accurate_counts(
    conn, settings, fetcher, source_id, monkeypatch,
):
    """The end-to-end "perfect completion" story: a request that yields
    partway through must NOT require the admin to manually resubmit
    anything -- the SAME request, re-claimed on a later poll (simulated
    here as a second direct call, exactly like the daemon's own next loop
    iteration would do), picks up exactly where it left off and eventually
    reaches genuine COMPLETED with the true, non-regressed final counts --
    not the misleading "COMPLETED after 1 of 9" from the real incident this
    whole mechanism exists to prevent."""
    import time as time_module

    real_monotonic = time_module.monotonic
    clock = {"offset": 0.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: real_monotonic() + clock["offset"])
    monkeypatch.setattr(cli, "_REQUEST_TIME_BUDGET_SECONDS", 1000)

    # Same proven hook as test_time_budget_yields_with_sources_left_for_a_
    # followup_request: jump the clock once this round's parsing has
    # nothing left to do (report.processed == 0) -- with a single source
    # and only 3 sample documents, that lands right after all 3 are
    # genuinely downloaded and parsed, but BEFORE the 2 additional discovery
    # confirmation passes that would otherwise mark the source (and
    # therefore the whole request) naturally exhausted.
    original_run_parsing = pipeline.run_parsing

    def parsing_that_exhausts_the_clock(*args, **kwargs):
        report = original_run_parsing(*args, **kwargs)
        if kwargs.get("source_id") == source_id and report.processed == 0:
            clock["offset"] = 100_000
        return report

    monkeypatch.setattr(pipeline, "run_parsing", parsing_that_exhausts_the_clock)

    req = {
        "id": 1, "kind": "extraction", "state_name": None, "district_name": None, "department_filter": None,
    }
    first_slice_client = _CompletionCapturingHttpxClient()
    cli._run_claimed_request(conn, settings, fetcher, first_slice_client, "https://example.invalid", {}, req)

    # First slice: yielded, not completed -- but all 3 documents' real work
    # already landed and was reported, even though the source isn't yet
    # confirmed "complete" (sources_completed stays 0 until 2 more empty
    # discovery passes confirm exhaustion -- see the sibling test above).
    assert first_slice_client.complete_calls == []
    yield_call = [c for c in first_slice_client.progress_calls if c.get("yield_and_requeue")][0]
    assert yield_call["documents_parsed"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3

    # Simulate the daemon's next poll: budget restored to normal, clock back
    # to real time, the clock-jumping hook removed (it would otherwise
    # re-trigger on this slice's own first exhausted parse call), request
    # re-claimed and processed again from scratch (fresh local variables) --
    # exactly what happens on a real resume.
    monkeypatch.setattr(cli, "_REQUEST_TIME_BUDGET_SECONDS", 30 * 60)
    monkeypatch.setattr(pipeline, "run_parsing", original_run_parsing)
    clock["offset"] = 0.0
    second_slice_client = _CompletionCapturingHttpxClient()
    cli._run_claimed_request(conn, settings, fetcher, second_slice_client, "https://example.invalid", {}, req)

    # Now genuinely done: a real /complete call, with the TRUE cumulative
    # total (all 3), not just what happened in this second slice alone, and
    # not regressed back below what the first slice had already achieved.
    assert len(second_slice_client.complete_calls) == 1
    assert second_slice_client.complete_calls[0]["ok"] is True
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3

    final_progress = second_slice_client.progress_calls[-1]
    assert final_progress["documents_parsed"] == 3
    assert final_progress["documents_downloaded"] == 3
