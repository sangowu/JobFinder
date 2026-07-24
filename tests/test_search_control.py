import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from jobradar import server
from jobradar.agent import SearchCancelled


@pytest.fixture(autouse=True)
def reset_search_control():
    server._search_cancel.clear()
    server._search_run_gate.set()
    server._set_search_state("idle")
    yield
    server._search_cancel.clear()
    server._search_run_gate.set()
    server._set_search_state("idle")


def test_pause_and_resume_are_mutually_exclusive_states(monkeypatch):
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    server._set_search_state("running")

    assert server.pause_search() == {"status": "paused"}
    assert server.search_status()["state"] == "paused"
    assert not server._search_run_gate.is_set()

    with pytest.raises(HTTPException) as exc:
        server.pause_search()
    assert exc.value.status_code == 409

    assert server.resume_search() == {"status": "running"}
    assert server.search_status()["state"] == "running"
    assert server._search_run_gate.is_set()


def test_paused_checkpoint_waits_until_resume():
    server._search_run_gate.clear()
    finished = threading.Event()

    worker = threading.Thread(target=lambda: (server._search_checkpoint(), finished.set()))
    worker.start()
    time.sleep(0.05)
    assert not finished.is_set()

    server._search_run_gate.set()
    worker.join(timeout=1)
    assert finished.is_set()


def test_stop_releases_pause_and_cancels_checkpoint(monkeypatch):
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    server._set_search_state("paused")
    server._search_run_gate.clear()

    assert server.stop_search() == {"status": "stopping"}
    assert server._search_run_gate.is_set()
    assert server._search_cancel.is_set()
    with pytest.raises(SearchCancelled):
        server._search_checkpoint()


def test_cannot_control_idle_search():
    for action in (server.pause_search, server.resume_search, server.stop_search):
        with pytest.raises(HTTPException) as exc:
            action()
        assert exc.value.status_code == 409


def test_search_controls_exist_and_pause_resume_visibility_is_mutually_exclusive():
    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="pause-search-btn"' in html
    assert 'id="resume-search-btn"' in html
    assert 'id="stop-search-btn"' in html
    assert 'pauseBtn.classList.toggle("hidden", state !== "running")' in html
    assert 'resumeBtn.classList.toggle("hidden", state !== "paused")' in html


def test_start_search_keeps_existing_jobs_until_sse_updates_them():
    html = (Path(__file__).parents[1] / "jobradar" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    start_search = html.split("async function startSearch()", 1)[1].split(
        "function startSearchTimer", 1
    )[0]

    assert "_prevSearchKeys = new Set(JOBS.map" in start_search
    assert "JOBS = []" not in start_search
    assert "activeKey = null" not in start_search
    assert "if (existing >= 0) JOBS[existing] = job;" in html
