"""app/assets.py: pinned, verified, streamed downloads and the GPU/ffmpeg packs.

Offline: the one HTTP server here is a local stand-in on 127.0.0.1.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import assets, config

windows_only = pytest.mark.skipif(os.name != "nt", reason="the packs are Windows-only")


# ------------------------------------------------------------ local server

class _Handler(BaseHTTPRequestHandler):
    payload = b""
    drop_first = False
    ignore_range = False
    requests: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        cls = type(self)
        rng = self.headers.get("Range")
        cls.requests.append(rng)
        start = int(rng.split("=")[1].split("-")[0]) if rng and not cls.ignore_range else 0
        body = cls.payload[start:]
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(cls.payload) - 1}/{len(cls.payload)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if cls.drop_first:
            cls.drop_first = False
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


@pytest.fixture
def server():
    _Handler.payload = os.urandom(3 * 1024 * 1024 + 4321)
    _Handler.drop_first = False
    _Handler.ignore_range = False
    _Handler.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/file.bin", _Handler
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


# ----------------------------------------------------------------- download

def test_download_verifies_and_reports_progress(server, tmp_path):
    url, h = server
    seen = []
    out = assets.download(url, tmp_path / "f.bin", hashlib.sha256(h.payload).hexdigest(),
                          on_progress=lambda d, t: seen.append((d, t)))
    assert out.read_bytes() == h.payload
    assert seen[-1] == (len(h.payload), len(h.payload))
    assert not (tmp_path / "f.bin.part").exists()


def test_download_resumes_after_a_dropped_connection(server, tmp_path, monkeypatch):
    url, h = server
    h.drop_first = True
    monkeypatch.setattr(assets.time, "sleep", lambda s: None)
    out = assets.download(url, tmp_path / "f.bin", hashlib.sha256(h.payload).hexdigest())
    assert out.read_bytes() == h.payload
    assert h.requests[0] is None and h.requests[1].startswith("bytes=")   # resumed, not restarted


def test_download_restarts_when_server_ignores_range(server, tmp_path, monkeypatch):
    url, h = server
    h.drop_first = True
    h.ignore_range = True
    monkeypatch.setattr(assets.time, "sleep", lambda s: None)
    out = assets.download(url, tmp_path / "f.bin", hashlib.sha256(h.payload).hexdigest())
    assert out.read_bytes() == h.payload


def test_download_rejects_wrong_hash_and_leaves_nothing(server, tmp_path):
    url, _ = server
    with pytest.raises(assets.PackError) as err:
        assets.download(url, tmp_path / "f.bin", "0" * 64)
    assert "damaged" in str(err.value)
    assert list(tmp_path.iterdir()) == []


def test_friendly_messages_are_plain():
    import urllib.error
    assert "internet" in assets._friendly(urllib.error.URLError("x"))
    assert "disk is full" in assets._friendly(OSError(28, "No space left"))
    assert assets._friendly(assets.PackError("Plain.")) == "Plain."


# ------------------------------------------------------------------- pins

def test_cublas_pin_is_consistent():
    w = assets.CUBLAS_WHEEL
    assert w["filename"] in w["url"] and w["version"] in w["filename"]
    assert len(w["sha256"]) == 64 and w["url"].startswith("https://files.pythonhosted.org/")
    assert assets.GPU_PACK_SIZE_MB == round(w["size"] / 1048576)
    assert assets.NVIDIA_LICENSE["url"].startswith("https://")
    assert "license" in assets.NVIDIA_LICENSE["text"].lower()


@pytest.mark.parametrize("system,machine,key", [
    ("Windows", "AMD64", "win64"), ("Windows", "ARM64", "winarm64"),
    ("Linux", "x86_64", "linux64"), ("Linux", "aarch64", "linuxarm64"),
    ("Darwin", "arm64", None), ("Windows", "x86", None)])
def test_ffmpeg_platform_keys(system, machine, key):
    assert assets.ffmpeg_platform_key(system, machine) == key


def test_ffmpeg_asset_urls_are_pinned():
    url, name, sha, size = assets.ffmpeg_asset("win64")
    assert "/releases/download/latest/" not in url and url.endswith(name)
    assert assets.FFMPEG_PIN["version"] in name


# ------------------------------------------------------------ status/state

def test_status_shape(monkeypatch):
    from app import hardware
    monkeypatch.setattr(hardware, "probe_gpus", lambda: [{"name": "NVIDIA Test GPU"}])
    st = assets.status()
    for key in ("gpu_pack_installed", "gpu_pack_needed", "gpu_name", "gpu_pack_size_mb",
                "gpu_pack_disk_mb", "gpu_pack_license", "ffmpeg_installed", "progress"):
        assert key in st
    assert st["gpu_name"] == "NVIDIA Test GPU"
    assert st["gpu_pack_license"]["url"] == assets.NVIDIA_LICENSE["url"]
    assert set(st["progress"]) >= {"busy", "percent", "message", "bytes_done", "bytes_total"}


def test_only_one_download_at_a_time():
    assert assets._claim("gpu", "x")
    try:
        assert not assets._claim("ffmpeg", "y")
        assert assets.install_ffmpeg()["ok"] is False or os.name != "nt"
    finally:
        assets._set(busy=False)


# ------------------------------------------------------------------ GPU pack

def _fake_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("nvidia/cublas/bin/cublas64_12.dll", b"MZ-cublas")
        zf.writestr("nvidia/cublas/bin/cublasLt64_12.dll", b"MZ-cublasLt")
        zf.writestr("nvidia/cublas/bin/nvblas64_12.dll", b"MZ-not-needed")
        zf.writestr("nvidia/cublas/include/cublas.h", b"header")
        zf.writestr("nvidia_cublas_cu12-12.9.2.10.dist-info/licenses/License.txt", b"NVIDIA EULA")
    return path


@pytest.fixture
def fresh_runtime():
    shutil.rmtree(config.RUNTIME_DIR, ignore_errors=True)
    yield config.RUNTIME_DIR
    shutil.rmtree(config.RUNTIME_DIR, ignore_errors=True)


@windows_only
def test_install_gpu_pack_extracts_only_what_is_needed(monkeypatch, fresh_runtime, tmp_path):
    wheel_src = _fake_wheel(tmp_path / "w.whl")
    calls = {}

    def fake_download(url, dest, sha256, on_progress=None, size_hint=0, attempts=4):
        calls["url"], calls["sha"] = url, sha256
        if on_progress:
            on_progress(size_hint // 2, size_hint)
        shutil.copy2(wheel_src, dest)
        return dest

    monkeypatch.setattr(assets, "download", fake_download)
    monkeypatch.setattr(assets, "_pypi_file",
                        lambda *a: (assets.CUBLAS_WHEEL["url"], assets.CUBLAS_WHEEL["sha256"]))
    monkeypatch.setattr(assets, "_need_space", lambda *a: None)
    monkeypatch.setattr(assets, "ffmpeg_platform_key", lambda *a: "win64")

    res = assets.install_gpu_pack()
    assert res["ok"], res
    assert calls["sha"] == assets.CUBLAS_WHEEL["sha256"]
    names = sorted(p.name for p in config.CUDA_DIR.iterdir())
    assert names == ["NVIDIA-LICENSE.txt", "cublas64_12.dll", "cublasLt64_12.dll", "pack.json"]
    assert json.loads((config.CUDA_DIR / "pack.json").read_text())["version"] == "12.9.2.10"
    assert assets.gpu_pack_installed()
    assert not assets.state()["busy"]
    assert not any((fresh_runtime / ".downloads").iterdir())          # wheel deleted


@windows_only
def test_install_gpu_pack_refuses_a_different_file_on_pypi(monkeypatch, fresh_runtime):
    monkeypatch.setattr(assets, "_pypi_file", lambda *a: ("https://x/y.whl", "f" * 64))
    monkeypatch.setattr(assets, "_need_space", lambda *a: None)
    monkeypatch.setattr(assets, "ffmpeg_platform_key", lambda *a: "win64")
    monkeypatch.setattr(assets, "download", lambda *a, **k: pytest.fail("must not download"))
    res = assets.install_gpu_pack()
    assert not res["ok"] and "different file" in res["error"]
    assert not assets.state()["busy"]


def test_need_space_message_is_plain(tmp_path):
    with pytest.raises(assets.PackError) as err:
        assets._need_space(tmp_path, 1 << 60)
    assert "free space" in str(err.value) and "GB" in str(err.value)


def test_remove_gpu_pack(fresh_runtime):
    config.CUDA_DIR.mkdir(parents=True)
    for n in assets.CUBLAS_DLLS:
        (config.CUDA_DIR / n).write_bytes(b"x")
    assert assets.gpu_pack_installed()
    res = assets.remove_gpu_pack()
    assert res["ok"] and not res["restart_needed"]
    assert not assets.gpu_pack_installed() and not config.CUDA_DIR.exists()


@windows_only
def test_remove_gpu_pack_with_a_loaded_dll(fresh_runtime):
    """After a GPU transcription the DLL is loaded and cannot be deleted; it
    must still stop counting as installed, and go away on a later look."""
    import ctypes
    src = Path(sys.base_prefix) / "vcruntime140_1.dll"
    if not src.exists():
        pytest.skip("no small DLL to load")
    config.CUDA_DIR.mkdir(parents=True)
    dll = config.CUDA_DIR / "cublas64_12.dll"
    shutil.copy2(src, dll)
    (config.CUDA_DIR / "cublasLt64_12.dll").write_bytes(b"x")
    handle = ctypes.WinDLL(str(dll))
    try:
        res = assets.remove_gpu_pack()
        assert res["ok"]
        assert not assets.gpu_pack_installed()
        assert res["restart_needed"] and "Restart" in res["message"]
    finally:
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(handle._handle))
    assets.status()                                     # empties the trash
    assert not (config.RUNTIME_DIR / ".trash").exists()


# -------------------------------------------------------------------- ffmpeg

@windows_only
def test_install_ffmpeg_repair(monkeypatch, fresh_runtime, tmp_path):
    archive = tmp_path / "ff.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ffmpeg-n7/bin/ffmpeg.exe", b"MZ1")
        zf.writestr("ffmpeg-n7/bin/ffprobe.exe", b"MZ2")
        zf.writestr("ffmpeg-n7/bin/ffplay.exe", b"MZ3")
        zf.writestr("ffmpeg-n7/LICENSE.txt", b"GPL v3")

    def fake_download(url, dest, sha256, on_progress=None, size_hint=0, attempts=4):
        assert url == assets.ffmpeg_asset("win64")[0]
        shutil.copy2(archive, dest)
        return dest

    monkeypatch.setattr(assets, "download", fake_download)
    monkeypatch.setattr(assets, "_need_space", lambda *a: None)
    monkeypatch.setattr(assets, "ffmpeg_platform_key", lambda *a: "win64")
    res = assets.install_ffmpeg()
    assert res["ok"], res
    dest = config.RUNTIME_DIR / "bin"
    assert sorted(p.name for p in dest.iterdir()) == \
        ["FFMPEG-LICENSE.txt", "FFMPEG-VERSION.txt", "ffmpeg.exe", "ffprobe.exe"]
    assert assets.FFMPEG_PIN["version"] in (dest / "FFMPEG-VERSION.txt").read_text()
    assert res["message"] == "ffmpeg is installed."
