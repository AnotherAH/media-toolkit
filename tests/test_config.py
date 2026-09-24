"""Settings file, data folder and environment set-up (app/config.py)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    """A private config.json for the test; the session's one is untouched."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "BACKUP_PATH", tmp_path / "config.json.bak")
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "NOTICE", "")
    yield tmp_path
    config._cache = None


def test_contract_defaults():
    d = config.DEFAULTS
    expected = {"dl_mode": "video", "dl_quality": "1080", "dl_compatible": True,
                "dl_audio_codec": "mp3", "dl_container": "mp4", "dl_subtitles": "none",
                "dl_subtitle_langs": "en", "dl_auto_subs": False, "dl_embed_subs": True,
                "dl_split_chapters": False, "dl_max_comments": 200, "dl_write_thumbnail": False,
                "dl_write_link": False, "lv_audio": False, "lv_quality": "best",
                "lv_container": "mp4", "lv_max": "", "lv_split": "", "lv_wait": False,
                "lv_wait_hours": 3, "transcript_language": "", "notify_prompted": False,
                "filename_preset": "title_id", "set_mtime": False, "external_downloader": ""}
    for key, value in expected.items():
        assert d[key] == value, key
    assert d["output_template"] == config.FILENAME_PRESETS["title_id"]


def test_presets():
    assert config.FILENAME_PRESETS == {
        "title_id": "%(title).180B [%(id)s].%(ext)s",
        "title": "%(title).200B.%(ext)s",
        "channel_title": "%(uploader,channel|Unknown).60B - %(title).150B.%(ext)s",
        "date_title": "%(upload_date>%Y-%m-%d|)s %(title).180B.%(ext)s",
    }


@pytest.mark.parametrize("template,ok", [
    ("%(title)s.%(ext)s", True), ("%(uploader)s/%(title)s.%(ext)s", True),
    ("../%(title)s.%(ext)s", False), ("a/../../b.%(ext)s", False), ("C:\\x\\%(id)s", False),
    ("\\\\server\\share\\x", False), ("/etc/x", False), ("", False), ("x" * 500, False),
])
def test_output_template_ok(template, ok):
    assert config.output_template_ok(template) is ok


def test_save_is_atomic_and_keeps_a_backup(cfgfile):
    cfg = config.save({"rate_limit": "5M", "not_a_key": 1})
    assert cfg["rate_limit"] == "5M" and "not_a_key" not in cfg
    on_disk = json.loads((cfgfile / "config.json").read_text("utf-8"))
    assert on_disk["rate_limit"] == "5M"
    assert (cfgfile / "config.json.bak").exists()
    assert not (cfgfile / "config.json.tmp").exists()


def test_damaged_config_falls_back_to_the_backup(cfgfile):
    config.save({"proxy": "http://127.0.0.1:3128", "setup_complete": True})
    (cfgfile / "config.json").write_text('{"proxy": "http://127.0.0.1:31', encoding="utf-8")
    config._cache = None
    cfg = config.get()
    assert cfg["proxy"] == "http://127.0.0.1:3128" and cfg["setup_complete"] is True
    assert "last good copy" in config.NOTICE
    assert list(cfgfile.glob("config.corrupt-*.json"))


def test_damaged_config_without_backup_uses_defaults_and_says_so(cfgfile):
    (cfgfile / "config.json").write_text("", encoding="utf-8")
    cfg = config.get()
    assert cfg["setup_complete"] is False
    assert "default settings" in config.NOTICE


def test_notepad_bom_still_loads(cfgfile):
    (cfgfile / "config.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"rate_limit": "2M"}).encode())
    assert config.get()["rate_limit"] == "2M"
    assert config.NOTICE == ""


def test_dangerous_stored_values_are_not_trusted(cfgfile):
    (cfgfile / "config.json").write_text(json.dumps({
        "external_downloader": "C:\\evil\\curl.exe",
        "output_template": "..\\..\\Startup\\%(id)s.bat"}), encoding="utf-8")
    cfg = config.get()
    assert cfg["external_downloader"] == ""
    assert cfg["output_template"] == config.FILENAME_PRESETS["title_id"]


def test_an_old_custom_template_becomes_the_custom_preset(cfgfile):
    (cfgfile / "config.json").write_text(json.dumps({"output_template": "%(id)s.%(ext)s"}),
                                         encoding="utf-8")
    cfg = config.get()
    assert cfg["filename_preset"] == "custom" and cfg["output_template"] == "%(id)s.%(ext)s"


def test_ensure_dirs_never_raises(cfgfile, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    config.save({"download_dir": str(blocker / "downloads"), "transcript_dir": str(tmp_path / "t")})
    problems = config.ensure_dirs()
    assert set(problems) == {"download_dir"}
    assert problems["download_dir"].startswith("Can't use this folder")
    assert (tmp_path / "t").is_dir()
    config.FOLDER_PROBLEMS.clear()


def test_folder_reason():
    assert config.folder_reason(PermissionError(13, "denied")) == "access denied"
    assert config.folder_reason(FileNotFoundError(2, "gone")) == \
        "the drive or network location isn't available"
    full = OSError(28, "No space left on device")
    assert config.folder_reason(full) == "the drive is full"


def test_proxy_env_is_set_and_restored(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "_saved_env", None)
    config.apply_proxy_env("http://127.0.0.1:3128")
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert "127.0.0.1" in os.environ["NO_PROXY"]
    config.apply_proxy_env("socks5://127.0.0.1:1080")      # not exported, restored instead
    assert "HTTPS_PROXY" not in os.environ and "NO_PROXY" not in os.environ
    config.apply_proxy_env("")
    assert "HTTP_PROXY" not in os.environ


def test_bootstrap_turns_off_third_party_tracking(monkeypatch):
    for k in ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN", "HF_HOME"):
        monkeypatch.delenv(k, raising=False)
    config.bootstrap()
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert Path(os.environ["HF_HOME"]).parent == config.DATA_ROOT


# ----------------------------------------------------------- data folder

_PROBE = r"""
import os, sys, json
sys.frozen = True
sys.executable = os.environ["FAKE_EXE"]
sys.path.insert(0, os.environ["REPO"])
import app.config as c
print(json.dumps({"data": str(c.DATA_ROOT), "portable": c.PORTABLE}))
"""


def _frozen_data_root(exe_dir: Path, local: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "MEDIA_TOOLKIT_HOME"}
    env.update(FAKE_EXE=str(exe_dir / "MediaToolkit.exe"), REPO=str(ROOT),
               LOCALAPPDATA=str(local))
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(os.name != "nt", reason="LOCALAPPDATA layout is Windows-only")
def test_installed_build_uses_local_app_data(tmp_path):
    exe_dir, local = tmp_path / "Program", tmp_path / "Local"
    exe_dir.mkdir()
    got = _frozen_data_root(exe_dir, local)
    assert Path(got["data"]) == local / "Media Toolkit" and got["portable"] is False


@pytest.mark.skipif(os.name != "nt", reason="LOCALAPPDATA layout is Windows-only")
def test_portable_txt_keeps_data_beside_the_exe(tmp_path):
    exe_dir, local = tmp_path / "Stick" / "Media Toolkit", tmp_path / "Local"
    exe_dir.mkdir(parents=True)
    (exe_dir / "portable.txt").write_text("")
    got = _frozen_data_root(exe_dir, local)
    assert Path(got["data"]) == exe_dir / "data" and got["portable"] is True
    assert not (local / "Media Toolkit").exists()


def test_media_toolkit_home_wins(data_root):
    assert config.DATA_ROOT == data_root.resolve()
