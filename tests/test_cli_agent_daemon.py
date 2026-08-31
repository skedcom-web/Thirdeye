"""cli.py::cmd_agent_daemon -- the --idle-exit-after auto-stop behavior.

Next Phase Blueprint's agent lifecycle ask: --daemon should exit on its own
once the queue has been empty for a few consecutive polls, instead of
looping forever until Ctrl+C (see cmd_agent_daemon's own docstring for why
"launch a process on the admin's separate machine" isn't achievable here --
this is the part that is)."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from goengine import cli


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
