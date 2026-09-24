"""Save as and Save changes for a transcript opened from 'Recent transcripts'
by its stem, when no job for it is left in the queue or the history."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as appmain

BASE = "http://127.0.0.1:8931"


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setattr(appmain, "DEV_NO_TOKEN", False)
    monkeypatch.setattr(appmain, "REMOTE", False)


@pytest.fixture
def client():
    c = TestClient(appmain.app, base_url=BASE)
    c.headers["X-MT-Token"] = appmain.TOKEN
    return c


@pytest.fixture
def sidecar(tmp_path):
    before = config.get()
    folder = tmp_path / "Transcripts"
    folder.mkdir()
    config.save({"transcript_dir": str(folder)})
    stem = "گفتگو (قسمت ۱)"
    segs = [{"start": 0.0, "end": 2.5, "text": "سلام."},
            {"start": 23.0, "end": 25.0, "text": "خداحافظ."}]
    side = {"version": 1, "stem": stem, "created": 1_700_000_000.0, "formats": ["txt"],
            "meta": {"title": stem, "url": "", "duration": 25}, "detail": {"source": "whisper"},
            "stats": {"words": 2}, "segments": segs}
    (folder / f"{stem}.mt.json").write_text(json.dumps(side, ensure_ascii=False), encoding="utf-8")
    (folder / f"{stem}.txt").write_text("سلام. خداحافظ.", encoding="utf-8")
    yield folder, stem
    config.save(before)


def test_export_by_stem_writes_once_next_to_the_sidecar(client, sidecar):
    folder, stem = sidecar
    r = client.post(f"/api/transcripts/{stem}/export", json={"format": "srt"})
    assert r.status_code == 200 and r.json()["created"] is True
    path = Path(r.json()["path"])
    assert path == folder / f"{stem}.srt" and "00:00:23,000" in path.read_text("utf-8")
    again = client.post(f"/api/transcripts/{stem}/export", json={"format": "srt"}).json()
    assert again["created"] is False
    assert client.post(f"/api/transcripts/{stem}/export", json={"format": "exe"}).status_code == 400


def test_save_text_by_stem_rewrites_the_txt(client, sidecar):
    folder, stem = sidecar
    r = client.post(f"/api/transcripts/{stem}/save-text", json={"text": "ویرایش شد."})
    assert r.status_code == 200
    assert (folder / f"{stem}.txt").read_text("utf-8") == "ویرایش شد."
    assert client.post(f"/api/transcripts/{stem}/save-text", json={"text": 5}).status_code == 400


@pytest.mark.parametrize("stem", ["..", "..\\..\\x", "missing", "a/b"])
def test_stem_writes_cannot_leave_the_folder(client, sidecar, stem):
    assert client.post(f"/api/transcripts/{stem}/export", json={"format": "txt"}).status_code == 404
    assert client.post(f"/api/transcripts/{stem}/save-text", json={"text": "x"}).status_code == 404


def test_stem_writes_need_the_token(sidecar):
    _, stem = sidecar
    bare = TestClient(appmain.app, base_url=BASE)
    assert bare.post(f"/api/transcripts/{stem}/save-text", json={"text": "x"}).status_code == 403
