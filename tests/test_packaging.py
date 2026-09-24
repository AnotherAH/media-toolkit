"""Build, installer and licence invariants.

These guard decisions that are easy to undo by accident in a later edit: one
version number everywhere, no GPL code and no NVIDIA library file in the
frozen build, the notices beside the exe, and an uninstaller that never
deletes downloads.
"""
from __future__ import annotations

import ast
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build            # noqa: E402  (tools/)
import fetch_ffmpeg     # noqa: E402
import third_party      # noqa: E402
import verify_build     # noqa: E402
import release_notes    # noqa: E402
import verify_gpu       # noqa: E402

import app              # noqa: E402

SPEC = (ROOT / "MediaToolkit.spec").read_text("utf-8")
ISS = (ROOT / "installer" / "MediaToolkit.iss").read_text("utf-8")


def spec_literal(name: str):
    for node in ast.parse(SPEC).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


# ------------------------------------------------------------------ version

def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", app.__version__)


def test_build_script_reads_the_same_version():
    assert build.version() == app.__version__


def test_spec_reads_version_from_app_init():
    # The spec must take the version from app/__init__.py, not hard-code one.
    assert 'ROOT / "app" / "__init__.py"' in SPEC
    assert "version=version_file()" in SPEC
    pattern = re.search(r"re\.search\(r'([^']+)'", SPEC).group(1)
    assert re.search(pattern, (ROOT / "app" / "__init__.py").read_text("utf-8")).group(1) \
        == app.__version__


def test_installer_has_no_hard_coded_version():
    assert not re.search(r'#define\s+AppVersion\s+"\d', ISS)
    assert 'GetStringFileInfo' in ISS                     # falls back to the exe's resource
    assert "OutputBaseFilename=MediaToolkit-Setup-{#AppVersion}" in ISS


def test_build_passes_version_to_installer():
    src = (ROOT / "tools" / "build.py").read_text("utf-8")
    assert 'f"/DAppVersion={ver}"' in src


def test_changelog_mentions_current_version():
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("no CHANGELOG.md yet")
    assert re.search(rf"^#+ .*\b{re.escape(app.__version__)}\b", changelog.read_text("utf-8"), re.M), \
        "CHANGELOG.md has no section for the current version (the release notes come from it)"


CHANGELOG_SAMPLE = """# Changelog

## [1.2.0] - 2026-09-30

### Added
- Persian: رونویس · notes

## 1.1.1

- older
"""


def test_release_notes_take_exactly_one_section():
    notes = release_notes.section(CHANGELOG_SAMPLE, "1.2.0")
    assert notes.startswith("### Added") and "رونویس" in notes
    assert "older" not in notes and "1.1.1" not in notes
    assert release_notes.section(CHANGELOG_SAMPLE, "1.1.1").strip() == "- older"
    with pytest.raises(LookupError):
        release_notes.section(CHANGELOG_SAMPLE, "1.2")          # never a prefix match


def test_release_notes_file_is_utf8(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG_SAMPLE, "utf-8")
    monkeypatch.setattr(release_notes, "ROOT", tmp_path)
    out = tmp_path / "notes.md"
    assert release_notes.main(["release_notes.py", "v1.2.0", str(out)]) == 0
    assert "رونویس" in out.read_text("utf-8")


def test_release_workflow_checks_tag_against_version():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8")
    assert "__version__" in wf and "GITHUB_REF_NAME" in wf


# ------------------------------------------------------------- licence (LEG-1)

def test_spec_excludes_gpl_packages():
    excludes = spec_literal("GPL_EXCLUDES")
    assert "av" in excludes and "mutagen" in excludes
    assert "GPL_EXCLUDES +" in SPEC                        # actually passed to Analysis


def test_spec_does_not_collect_pyav_libraries():
    loop = re.search(r'for pkg in \(([^)]*)\):\s*\n\s*binaries \+=', SPEC).group(1)
    assert '"av"' not in loop
    assert '"ctranslate2"' in loop


@pytest.mark.parametrize("dest", ["ctranslate2/cudnn64_9.dll", "ctranslate2\\cudnn64_9.dll",
                                  "cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll"])
def test_spec_filters_nvidia_dlls(dest):
    pattern = re.search(r'NVIDIA_DLL = re\.compile\(r"([^"]+)"', SPEC).group(1)
    assert re.search(pattern, dest, re.I)


@pytest.mark.parametrize("dest", ["ctranslate2/ctranslate2.dll", "ctranslate2/libiomp5md.dll",
                                  "onnxruntime/capi/onnxruntime.dll"])
def test_spec_keeps_other_dlls(dest):
    pattern = re.search(r'NVIDIA_DLL = re\.compile\(r"([^"]+)"', SPEC).group(1)
    assert not re.search(pattern, dest, re.I)


def test_spec_ships_the_whole_stdlib_for_yt_dlp_updates():
    # A yt-dlp downloaded later may import a stdlib module today's does not.
    assert "hiddenimports += stdlib_modules()" in SPEC
    # collect_submodules imports what it lists: never a package's __main__.
    assert 'p == "__main__"' in SPEC


@pytest.mark.parametrize("path,nvidia,gpl", [
    (r"C:\a\_internal\ctranslate2\cudnn64_9.dll", True, False),
    (r"C:\home\runtime\cuda\cublasLt64_12.dll", True, False),
    (r"C:\a\_internal\av.libs\avcodec-62-0123abcd.dll", False, True),
    (r"C:\a\_internal\av.libs\libx265-0123.dll", False, True),
    (r"C:\a\_internal\ctranslate2\ctranslate2.dll", False, False),
    (r"C:\Windows\System32\nvcuda.dll", False, False)])
def test_verify_gpu_recognises_libraries(path, nvidia, gpl):
    assert bool(verify_gpu.NVIDIA_DLLS.search(path)) is nvidia
    assert bool(verify_gpu.GPL_DLLS.search(path)) is gpl


def test_verify_gpu_upload_is_valid_multipart(tmp_path):
    from email.parser import BytesParser
    sample = tmp_path / "speech.wav"
    sample.write_bytes(b"RIFF\x00\x01wave")
    body, ctype = verify_gpu.multipart(sample, {"model": "tiny"})
    msg = BytesParser().parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
    parts = {p.get_param("name", header="content-disposition"): p for p in msg.get_payload()}
    assert parts["options"].get_payload() == '{"model": "tiny"}'
    assert parts["file"].get_filename() == "speech.wav"
    assert parts["file"].get_payload(decode=True) == b"RIFF\x00\x01wave"


def test_requirements_have_no_nvidia_wheels_and_pin_versions():
    lines = [ln.strip() for ln in (ROOT / "requirements.txt").read_text("utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert not any(ln.lower().startswith("nvidia") for ln in lines)
    ytdlp = [ln for ln in lines if ln.startswith("yt-dlp")]
    assert ytdlp and ">=" in ytdlp[0]
    for ln in lines:
        if not ln.startswith("yt-dlp"):
            assert "~=" in ln, f"{ln} needs a compatible-release pin"


def test_build_requirements_pin_pyinstaller():
    text = (ROOT / "requirements-build.txt").read_text("utf-8")
    assert re.search(r"^pyinstaller==\d", text, re.M)


def test_gpu_requirements_match_the_in_app_pack():
    from app.assets import CUBLAS_WHEEL
    lines = [ln.strip() for ln in (ROOT / "requirements-gpu.txt").read_text("utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines == [f"nvidia-cublas-cu12=={CUBLAS_WHEEL['version']}"]


# ------------------------------------------------------------------ notices

@pytest.fixture(scope="module")
def notices_text(tmp_path_factory) -> str:
    bin_dir = tmp_path_factory.mktemp("bin")
    (bin_dir / "FFMPEG-LICENSE.txt").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 3", "utf-8")
    (bin_dir / "FFMPEG-VERSION.txt").write_text(
        fetch_ffmpeg.version_text(fetch_ffmpeg.pin(), "win64"), "utf-8")
    tops = {"yt_dlp", "fastapi", "ctranslate2", "onnxruntime", "numpy", "certifi",
            "mutagen", "av", "av.libs", "nvidia"}
    return third_party.render(third_party.distributions_for(tops), bin_dir)


def test_notices_generator_runs(notices_text):
    assert notices_text.startswith("THIRD-PARTY NOTICES")
    assert len(notices_text) > 20_000


def test_notices_carry_intel_terms_in_full(notices_text):
    assert "Intel Simplified Software License" in notices_text
    assert "No reverse engineering, decompilation, or disassembly" in notices_text
    assert "Intel End User License Agreement for Developer Tools" in notices_text
    assert "prohibits reverse engineering" in notices_text


def test_notices_leave_out_what_is_not_shipped(notices_text):
    summary = notices_text.split("Summary", 1)[1].split("DOWNLOADED LATER", 1)[0]
    names = {line.split()[0].lower() for line in summary.splitlines() if line.strip() and
             not line.startswith("-")}
    assert "mutagen" not in names and "av" not in names
    assert not any(n.startswith("nvidia-") for n in names)        # no NVIDIA wheels
    assert "cudnn64_9.dll" not in notices_text


def test_notices_cover_what_is_compiled_into_ctranslate2(notices_text):
    # ctranslate2.dll carries NVIDIA's CUDA runtime, Thrust and spdlog; the
    # CUDA EULA asks for end-user terms that protect NVIDIA's rights.
    assert "NVIDIA CUDA runtime (cudart_static" in notices_text
    assert "Attachment A" in notices_text and "cudart_static.lib" in notices_text
    assert "Thrust" in notices_text and "spdlog" in notices_text


def test_native_entries_follow_the_packages_in_the_build():
    names = [n["name"] for n in third_party.native_components({"fastapi"})]
    assert not any("ctranslate2" in n or "curl" in n for n in names)
    assert any("Visual C++" in n for n in names)
    everything = [n["name"] for n in third_party.native_components()]
    assert any(n.startswith("libcurl-impersonate") for n in everything)


def test_notices_explain_ffmpeg_source(notices_text):
    pin = fetch_ffmpeg.pin()
    assert pin["version"] in notices_text
    assert pin["builds_commit"] in notices_text
    assert "Corresponding source" in notices_text


def test_notices_mention_runtime_downloads(notices_text):
    from app.assets import CUBLAS_WHEEL
    assert CUBLAS_WHEEL["version"] in notices_text
    assert "CC BY-NC-SA 4.0" in notices_text


def test_intel_license_texts_are_present():
    for name in ("intel-simplified-software-license.txt", "intel-eula-developer-tools.txt",
                 "intel-openmp-third-party-programs.txt"):
        assert (ROOT / "tools" / "licenses" / name).stat().st_size > 2000


# -------------------------------------------------------------------- ffmpeg

def test_fetch_ffmpeg_reads_the_same_pin_as_the_app():
    from app.assets import FFMPEG_PIN
    assert fetch_ffmpeg.pin() == FFMPEG_PIN


@pytest.mark.parametrize("key", ["win64", "winarm64"])
def test_installer_and_repair_write_the_same_ffmpeg_notes(key):
    from app.assets import ffmpeg_version_text
    assert fetch_ffmpeg.version_text(fetch_ffmpeg.pin(), key) == ffmpeg_version_text(key)


def test_ffmpeg_source_directions_are_complete():
    pin = fetch_ffmpeg.pin()
    assert re.fullmatch(r"[0-9a-f]{40}", pin["ffmpeg_commit"])       # a full, permanent commit
    assert re.fullmatch(r"[0-9a-f]{40}", pin["builds_commit"])
    assert pin["ffmpeg_commit"].startswith(pin["version"].rsplit("-g", 1)[-1])
    assert pin["version"].startswith("n" + pin["ffmpeg_release"])
    text = fetch_ffmpeg.version_text(pin, "win64")
    assert f"ffmpeg-{pin['ffmpeg_release']}.tar.xz" in text and pin["builds_commit"] in text


def test_ffmpeg_pin_is_a_release_branch_build():
    pin = fetch_ffmpeg.pin()
    assert re.fullmatch(r"n\d+\.\d+(\.\d+)?(-\d+-g[0-9a-f]+)?", pin["version"])
    assert "/releases/download/latest/" not in pin["base_url"]     # never a moving tag
    for name, sha, size in pin["assets"].values():
        assert pin["version"] in name
        assert re.fullmatch(r"[0-9a-f]{64}", sha) and size > 10_000_000


def _zip_with(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_fetch_ffmpeg_extracts_binaries_and_license(tmp_path):
    archive = _zip_with(tmp_path / "ff.zip", {
        "ffmpeg-x/bin/ffmpeg.exe": b"MZ1", "ffmpeg-x/bin/ffprobe.exe": b"MZ2",
        "ffmpeg-x/bin/ffplay.exe": b"MZ3", "ffmpeg-x/LICENSE.txt": b"GPL",
        "ffmpeg-x/doc/LICENSE.txt": b"not this one"})
    out = tmp_path / "out"
    out.mkdir()
    if sys.platform != "win32":
        pytest.skip("exe names are Windows-only")
    assert fetch_ffmpeg.extract(archive, out) == 2
    assert sorted(p.name for p in out.iterdir()) == ["FFMPEG-LICENSE.txt", "ffmpeg.exe", "ffprobe.exe"]
    assert (out / "FFMPEG-LICENSE.txt").read_bytes() == b"GPL"


def test_fetch_ffmpeg_replaces_a_build_that_is_not_the_pin(tmp_path):
    pin = fetch_ffmpeg.pin()
    for n in fetch_ffmpeg.exe_names():
        (tmp_path / n).write_bytes(b"x")
    assert not fetch_ffmpeg.already_have(tmp_path, pin["version"])        # no version file
    (tmp_path / "FFMPEG-VERSION.txt").write_text("FFmpeg N-1-gabc (GPL)\n", "utf-8")
    assert not fetch_ffmpeg.already_have(tmp_path, pin["version"])        # other build
    (tmp_path / "FFMPEG-VERSION.txt").write_text(fetch_ffmpeg.version_text(pin, "win64"), "utf-8")
    assert not fetch_ffmpeg.already_have(tmp_path, pin["version"])        # no GPL text
    (tmp_path / "FFMPEG-LICENSE.txt").write_text("GPL", "utf-8")
    assert fetch_ffmpeg.already_have(tmp_path, pin["version"])


# ------------------------------------------------------------ build outputs

def test_portable_zip_has_marker(tmp_path):
    app_dir = tmp_path / "MediaToolkit"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "MediaToolkit.exe").write_bytes(b"MZ")
    (app_dir / "_internal" / "x.pyd").write_bytes(b"x")
    out = tmp_path / "p.zip"
    build.make_zip(app_dir, out)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "MediaToolkit/portable.txt" in names
    assert "MediaToolkit/TERMS.txt" in names
    assert "MediaToolkit/MediaToolkit.exe" in names
    assert "MediaToolkit/_internal/x.pyd" in names


def test_checksums_file_is_what_sha256sum_reads(tmp_path):
    import hashlib
    a, b = tmp_path / "MediaToolkit-Setup-9.9.9.exe", tmp_path / "MediaToolkit-9.9.9-portable.zip"
    a.write_bytes(b"setup")
    b.write_bytes(b"zip")
    build.write_checksums([a, b], tmp_path / "SHA256SUMS.txt")
    data = (tmp_path / "SHA256SUMS.txt").read_bytes()
    assert b"\r" not in data                              # CRLF breaks `sha256sum -c`
    assert data.decode().splitlines() == [
        f"{hashlib.sha256(b'setup').hexdigest()}  {a.name}",
        f"{hashlib.sha256(b'zip').hexdigest()}  {b.name}"]


def test_verify_build_refuses_an_ffmpeg_that_is_not_the_pin(tmp_path):
    app_dir = tmp_path / "MediaToolkit"
    (app_dir / "bin").mkdir(parents=True)
    pin = fetch_ffmpeg.pin()
    (app_dir / "bin" / "FFMPEG-VERSION.txt").write_text("FFmpeg N-126250-gdea13cca4f (GPL)\n", "utf-8")
    assert any("but the pin is" in p for p in verify_build.check(app_dir, app.__version__))
    (app_dir / "bin" / "FFMPEG-VERSION.txt").write_text(fetch_ffmpeg.version_text(pin, "win64"), "utf-8")
    assert not any("pin" in p for p in verify_build.check(app_dir, app.__version__))


def test_build_fetches_ffmpeg_strictly():
    # A release must never fall back to whatever ffmpeg is on PATH: the
    # notices describe the pinned build only.
    src = (ROOT / "tools" / "build.py").read_text("utf-8")
    assert '"tools/fetch_ffmpeg.py", "--strict"' in src


def test_verify_build_flags_forbidden_content(tmp_path):
    app_dir = tmp_path / "MediaToolkit"
    for rel in ("_internal/av/__init__.pyc", "_internal/av.libs/libx264-165.dll",
                "_internal/ctranslate2/cudnn64_9.dll", "_internal/mutagen/x.py",
                "_internal/ctranslate2/ctranslate2.dll"):
        (app_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (app_dir / rel).write_bytes(b"x")
    problems = verify_build.check(app_dir, app.__version__)
    text = "\n".join(problems)
    assert "av/__init__.pyc" in text and "av.libs" in text and "cudnn64_9.dll" in text
    assert "mutagen" in text and "missing MediaToolkit.exe" in text
    assert "ctranslate2.dll" not in text.replace("cudnn64_9.dll", "")


# ---------------------------------------------------------------- installer

def test_installer_ships_license_and_notices_and_no_readme_page():
    assert '{#SourceDir}\\LICENSE.txt' in ISS
    assert '{#SourceDir}\\THIRD-PARTY-NOTICES.txt' in ISS
    assert "isreadme" not in ISS
    assert "LicenseFile=terms.txt" in ISS and (ROOT / "installer" / "terms.txt").exists()


def test_installer_is_per_user_only():
    assert "PrivilegesRequired=lowest" in ISS
    assert "PrivilegesRequiredOverridesAllowed" not in ISS
    assert "DefaultDirName={autopf}" in ISS


def test_uninstaller_never_deletes_downloads():
    code = ISS.split("[Code]", 1)[1]
    assert "DelTree(Dir, True" not in code                 # never the whole data folder
    deleted = re.findall(r"DeleteItem\(Dir, '([^']+)'", code)
    assert deleted, "the uninstall prompt deletes nothing"
    for name in deleted:
        assert not name.lower().startswith(("downloads", "transcripts", "*"))
    assert "models" in deleted and "config.json" in deleted
    assert "IDNO" in code                                   # silent uninstall keeps data


def test_upgrade_clears_stale_program_files():
    section = ISS.split("[InstallDelete]", 1)[1].split("[", 1)[0]
    assert '{app}\\_internal' in section


def test_batch_and_iss_files_use_crlf():
    for rel in ("setup.bat", "Start.bat", "installer/MediaToolkit.iss", "installer/terms.txt"):
        data = (ROOT / rel).read_bytes()
        assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n"), rel


# ---------------------------------------------------------------- workflows

@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflows_use_only_official_actions(name):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text("utf-8"))
    uses = []
    for job in doc["jobs"].values():
        for st in job.get("steps", []):
            if "uses" in st:
                uses.append(st["uses"])
    assert uses
    for u in uses:
        assert re.fullmatch(r"actions/(checkout|setup-python|upload-artifact)@v\d+", u), u
