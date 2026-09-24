"""The launcher (run.py): ports, single instance, quitting, the log file,
--diagnose and command-line output in the windowed build."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

import run


# ------------------------------------------------------------------ ports

def test_busy_port_is_an_error_not_someone_elses_window():
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        busy = other.getsockname()[1]
        with pytest.raises(OSError, match="already in use"):
            run.bind_socket("127.0.0.1", busy)


def test_default_port_prefers_the_usual_one_then_the_next():
    first = run.bind_socket("127.0.0.1", 0)
    try:
        p1 = first.getsockname()[1]
        assert run.PREFERRED_PORT <= p1 < run.PREFERRED_PORT + 40 or p1 > 0
        second = run.bind_socket("127.0.0.1", 0)
        try:
            assert second.getsockname()[1] != p1
        finally:
            second.close()
    finally:
        first.close()


def test_wait_started_notices_a_dead_server_thread():
    import threading

    class Server:
        started = False

    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    began = time.time()
    assert run.wait_started(Server(), t, timeout=10) is False
    assert time.time() - began < 1


# ------------------------------------------------------------ quitting

def _state(**kw):
    s = {"clients": 0, "last_seen": 0.0, "last_beat": 0.0, "seen_any": True, "goodbye_at": 0.0}
    s.update(kw)
    return s


def test_window_gone_rules():
    now = 1000.0
    # Still connected.
    assert not run.window_gone(_state(clients=1, last_seen=now - 60), None, 0, now)
    # Heartbeats stopped with no goodbye: wait the grace period.
    assert not run.window_gone(_state(last_seen=now - 5, last_beat=now - 5), None, 0, now)
    assert run.window_gone(_state(last_seen=now - 30, last_beat=now - 30), None, 0, now)
    # A goodbye followed by a heartbeat is a reload, not a close.
    reload = _state(goodbye_at=now - 8, last_beat=now - 2, last_seen=now - 2)
    assert not run.window_gone(reload, None, 0, now)
    # A goodbye with nothing after it is a close once GOODBYE_SECONDS pass.
    closed = _state(goodbye_at=now - 6, last_beat=now - 9, last_seen=now - 6)
    assert run.window_gone(closed, None, 0, now)
    assert not run.window_gone(_state(goodbye_at=now - 1, last_beat=now - 9,
                                      last_seen=now - 1), None, 0, now)
    # The window never showed up.
    assert not run.window_gone(_state(seen_any=False), None, now - 60, now)
    assert run.window_gone(_state(seen_any=False), None, now - 200, now)


# ------------------------------------------------------- single instance

def test_second_lock_on_the_same_folder_fails(tmp_path):
    a, b = run.InstanceLock(tmp_path), run.InstanceLock(tmp_path)
    assert a.acquire()
    try:
        assert not b.acquire()
    finally:
        a.release()
    assert b.acquire()
    b.release()


def test_find_running_ignores_a_stale_instance_file(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    (tmp_path / "instance.json").write_text(json.dumps({"port": dead, "pid": 1, "token": "x"}))
    began = time.time()
    assert run.find_running(tmp_path) is None
    assert time.time() - began < 5


def test_instance_file_is_removed_only_by_its_owner(tmp_path):
    run._write_instance(tmp_path, 8931, "tok")
    info = json.loads((tmp_path / "instance.json").read_text())
    assert info["port"] == 8931 and info["pid"] == os.getpid() and info["token"] == "tok"
    (tmp_path / "instance.json").write_text(json.dumps({"port": 1, "pid": -5, "token": "t"}))
    run._remove_instance(tmp_path)
    assert (tmp_path / "instance.json").exists()
    run._write_instance(tmp_path, 8931, "tok")
    run._remove_instance(tmp_path)
    assert not (tmp_path / "instance.json").exists()


def test_second_launch_reattaches_to_the_running_instance(monkeypatch, tmp_path):
    from app import config, shell
    opened = []
    monkeypatch.setattr(shell, "attach_console", lambda: True)
    monkeypatch.setattr(run, "_setup_log", lambda root, console: tmp_path / "app.log")
    monkeypatch.setattr(run, "_prepare_runtime", lambda: None)
    monkeypatch.setattr(run.InstanceLock, "acquire", lambda self: False)
    monkeypatch.setattr(run, "find_running", lambda root, wait=0: {"port": 8931, "pid": 1})
    monkeypatch.setattr(run, "open_ui", lambda url, browser, root: opened.append(url))
    monkeypatch.setattr(run, "_serve", lambda *a: pytest.fail("started a second server"))
    assert run.main([]) == 0
    assert opened == ["http://127.0.0.1:8931"]


# ------------------------------------------------------------------ log

def test_log_rotates_and_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "LOG_LIMIT", 2000)
    log = run._Log(tmp_path / "app.log")
    log.write("Cookie: SID=supersecret\n")
    log.write("https://www.youtube.com/watch?v=abc&token=abcdef\n")
    for _ in range(60):
        log.write("x" * 50 + "\n")
    log.fh.close()
    assert (tmp_path / "app.log.1").exists()
    text = (tmp_path / "app.log.1").read_text("utf-8") + (tmp_path / "app.log").read_text("utf-8")
    assert "supersecret" not in text and "abcdef" not in text
    assert "v=abc" in text
    assert (tmp_path / "app.log").stat().st_size <= 2000


# -------------------------------------------------------------- diagnose

def test_diagnose_writes_a_report_and_never_purges_without_repair(tmp_path, monkeypatch, capsys):
    from app import config, models
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(models, "purge", lambda name: pytest.fail("purged without --repair"))
    monkeypatch.setattr(models, "ensure", lambda name, on_progress=None, check=None: "x")
    assert run.diagnose("tiny", repair=False, console=True) == 0
    report = (tmp_path / "diagnose.txt").read_text("utf-8")
    assert "ensure        : ok" in report and "repair" not in report
    assert run.diagnose("..\\victim", repair=True, console=True) == 2
    assert "Unknown model" in (tmp_path / "diagnose.txt").read_text("utf-8")


def test_diagnose_repair_purges_first(tmp_path, monkeypatch, capsys):
    from app import config, models
    purged = []
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(models, "purge", lambda name: purged.append(name))
    monkeypatch.setattr(models, "ensure", lambda name, on_progress=None, check=None: "x")
    assert run.diagnose("tiny", repair=True, console=True) == 0
    assert purged == ["tiny"]


def test_diagnose_opens_the_report_when_there_is_no_console(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(run.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(run.os, "name", "nt")
    assert run._finish_report("hello", tmp_path, console=False, code=0) == 0
    assert opened == [str(tmp_path / "diagnose.txt")]


# ------------------------------------------------------ command line

def test_help_without_a_console_shows_a_message_box(monkeypatch):
    from app import shell
    shown = []
    monkeypatch.setattr(shell, "attach_console", lambda: False)
    monkeypatch.setattr(shell, "message_box", lambda title, text, info=False: shown.append(text))
    monkeypatch.setattr(sys, "stdout", None)
    assert run.main(["--help"]) == 0
    assert shown and "--diagnose" in shown[0] and "--server" in shown[0]
