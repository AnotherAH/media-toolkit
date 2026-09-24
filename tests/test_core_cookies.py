"""Sign-ins (app/cookies.py), the folder picker's encoding (app/folderpick.py)
and the app window's arguments (app/shell.py). Offline."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import cookies, folderpick, shell


# -------------------------------------------------------------- pasting

def test_header_without_a_site_is_refused(tmp_path):
    with pytest.raises(ValueError, match="which site"):
        cookies.import_text("SID=abc; HSID=def", tmp_path / "c.txt")
    assert not (tmp_path / "c.txt").exists()


@pytest.mark.parametrize("site,domain", [
    ("YouTube", ".youtube.com"), ("https://www.instagram.com/p/x", ".instagram.com"),
    ("m.tiktok.com", ".tiktok.com"), ("twitter.com", ".x.com"), ("x", ".x.com"),
    ("https://www.bbc.co.uk/iplayer", ".bbc.co.uk"), ("patreon", ".patreon.com"),
])
def test_site_domain(site, domain):
    assert cookies.site_domain(site) == domain


@pytest.mark.parametrize("site", ["", "localhost", "not a site", "javascript:alert(1)"])
def test_site_domain_rejects_nonsense(site):
    with pytest.raises(ValueError):
        cookies.site_domain(site)


def test_header_is_written_for_the_named_site_only(tmp_path):
    jar = tmp_path / "cookies.txt"
    out = cookies.import_text("Cookie: SID=g.a000SECRET; HSID=x; LOGIN_INFO=y", jar, site="YouTube")
    assert out["domain"] == ".youtube.com" and out["cookies"] == 3
    rows = cookies._read_jar(jar)
    assert {r[0] for r in rows} == {".youtube.com"}
    assert "instagram" not in jar.read_text("utf-8")
    assert {r[5] for r in rows} == {"SID", "HSID", "LOGIN_INFO"}


def test_pastes_merge_instead_of_overwriting(tmp_path):
    jar = tmp_path / "cookies.txt"
    cookies.import_text("sessionid=a", jar, site="instagram.com")
    cookies.import_text("auth_token=b; ct0=c", jar, site="x.com")
    cookies.import_text("sessionid=new", jar, site="instagram.com")
    rows = {(r[0], r[5]): r[6] for r in cookies._read_jar(jar)}
    assert rows == {(".instagram.com", "sessionid"): "new", (".x.com", "auth_token"): "b",
                    (".x.com", "ct0"): "c"}
    assert not (tmp_path / "cookies.txt.tmp").exists()


def test_netscape_paste_keeps_its_domains(tmp_path):
    jar = tmp_path / "cookies.txt"
    text = ("# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
            "#HttpOnly_.vimeo.com\tTRUE\t/\tTRUE\t1999999999\tvuid\tdef\n")
    out = cookies.import_text(text, jar)
    assert out["format"] == "netscape" and out["domains"] == ["vimeo.com", "youtube.com"]


def test_written_jar_loads_in_yt_dlp(tmp_path):
    from yt_dlp.cookies import YoutubeDLCookieJar
    jar = tmp_path / "cookies.txt"
    cookies.import_text("SID=abc", jar, site="youtube.com")
    loaded = YoutubeDLCookieJar(str(jar))
    loaded.load()
    assert [c.name for c in loaded] == ["SID"]


# ----------------------------------------------------- per-run copies

def test_private_copies_protect_the_users_file(tmp_path):
    jar = tmp_path / "cookies.txt"
    cookies.import_text("SID=abc", jar, site="youtube.com")
    a, b = cookies.private_copy(jar), cookies.private_copy(jar)
    assert a != b and Path(a).read_text("utf-8") == jar.read_text("utf-8")
    Path(a).write_text("\x00\x00 truncated by yt-dlp", encoding="utf-8")
    assert "SID" in jar.read_text("utf-8")
    cookies.discard_copy(a)
    cookies.discard_copy(jar)                      # never deletes the user's own file
    assert not Path(a).exists() and jar.exists()
    cookies.discard_copy(b)


def test_stale_copies_of_dead_processes_are_removed():
    root = cookies._COPIES.parent
    dead = root / "4294967291"                     # no such process
    dead.mkdir(parents=True, exist_ok=True)
    (dead / "x.txt").write_text("SID=secret")
    mine = cookies._COPIES
    mine.mkdir(parents=True, exist_ok=True)
    assert cookies.remove_stale_copies() >= 1
    assert not dead.exists() and mine.exists()


def test_pid_alive():
    assert cookies._pid_alive(os.getpid()) is True
    assert cookies._pid_alive(4294967291) is False


# ------------------------------------------------------ sign-in window

def test_login_url_cannot_be_a_browser_switch(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(cookies.subprocess, "Popen", lambda args, **k: started.append(args))
    monkeypatch.setattr(cookies, "_chromium_binaries", lambda: [("Edge", Path("msedge.exe"))])
    for bad in ("--remote-debugging-port=9222", "file:///C:/", "https://a b"):
        assert cookies.start_login_browser(tmp_path, bad)["ok"] is False
    assert started == []
    out = cookies.start_login_browser(tmp_path, "https://www.tiktok.com/login")
    assert out["ok"] is True
    args = started[0]
    assert "--remote-debugging-port=0" in args            # never a fixed, guessable port
    assert args[-2:] == ["--", "https://www.tiktok.com/login"]


def test_harvest_keeps_session_cookies_as_sessions_and_closes_the_window(tmp_path, monkeypatch):
    closed = []
    monkeypatch.setattr(cookies, "_devtools_endpoint", lambda profile, timeout=2.0: "ws://x")
    monkeypatch.setattr(cookies, "_cdp", lambda ws, method, timeout=10.0: {"cookies": [
        {"domain": ".youtube.com", "name": "SID", "value": "a", "path": "/", "secure": True,
         "expires": -1},
        {"domain": "www.instagram.com", "name": "sessionid", "value": "b", "path": "/",
         "secure": True, "expires": 1999999999.5}]})
    monkeypatch.setattr(cookies, "_close_login_browser", lambda p: closed.append(p) or True)
    jar = tmp_path / "cookies.txt"
    out = cookies.harvest_login_cookies(jar, tmp_path / "login-profile")
    assert out["ok"] and out["cookies"] == 2 and closed
    rows = {r[5]: r for r in cookies._read_jar(jar)}
    assert rows["SID"][4] == "0" and rows["sessionid"][4] == "1999999999"


def test_forget_removes_the_jar_and_the_signed_in_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(cookies, "_close_login_browser", lambda p: True)
    profile = tmp_path / cookies.LOGIN_PROFILE
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"x")
    jar = tmp_path / "cookies.txt"
    jar.write_text("x")
    out = cookies.forget(tmp_path, jar)
    assert not profile.exists() and not jar.exists()
    assert out["profile_left"] is False and len(out["removed"]) == 2


def test_advice_has_no_em_dashes():
    results = [{"browser": "chrome", "label": "Chrome", "ok": False,
                "error": "Couldn't read it. Close Chrome completely and try again.",
                "domains": [], "count": 0}]
    for text in (cookies._advice(results, []), cookies._advice([], []),
                 cookies._diagnose("dpapi failure", "edge")):
        assert "\u2014" not in text


# ---------------------------------------------------------- folder picker

@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"),
                    reason="Windows PowerShell only")
def test_picker_output_survives_non_ascii_names():
    """The picker prints the path from PowerShell. Persian and accented
    names used to come back as '????'."""
    path = "C:\\Users\\user\\فیلم\\Vidéos"
    script = folderpick._PRELUDE.split("Add-Type")[0] + "[Console]::Out.Write($env:MT_PICK_START)"
    assert folderpick._run_ps(script, path) == path


def test_known_folders_are_real_paths():
    if os.name != "nt":
        assert folderpick.known_folder("videos") == ""
        return
    videos = folderpick.known_folder("videos")
    assert videos and Path(videos).is_absolute()
    assert folderpick.known_folder("nonsense") == ""


# ------------------------------------------------------------ app window

def test_window_goes_through_the_proxy_without_credentials(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(shell, "find_browser", lambda: ("Edge", Path("msedge.exe")))
    monkeypatch.setattr(shell.subprocess, "Popen", lambda args, **k: launched.append(args))
    shell.open_window("http://127.0.0.1:8931", tmp_path / "w", proxy="http://user:pw@proxy:3128")
    assert "--proxy-server=http://proxy:3128" in launched[0]
    assert not any("pw" in a for a in launched[0])
    shell.open_window("http://127.0.0.1:8931", tmp_path / "w", proxy="--evil")
    assert not any(a.startswith("--proxy-server") for a in launched[1])


def test_message_box_falls_back_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(shell.os, "name", "posix")
    shell.message_box("Media Toolkit", "hello")
    assert "hello" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_helper_process_is_tied_to_the_app():
    proc = subprocess.Popen(["cmd", "/c", "exit 0"], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert shell.kill_with_app(proc) in (True, False)      # never raises
    finally:
        proc.wait(5)
