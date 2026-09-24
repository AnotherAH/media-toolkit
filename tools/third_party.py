"""Write THIRD-PARTY-NOTICES.txt for a frozen build.

The installer redistributes other people's code: Python itself, every package
PyInstaller collected, the native libraries those packages carry, and a
static ffmpeg. Most of those licences require their text to travel with the
binaries, the Intel ones require their full terms, and the GPL requires
directions to the corresponding source. This collects all of it into one
file, built from what the build actually contains rather than from a
hand-kept list that drifts.

Called from MediaToolkit.spec with the Analysis tables, or standalone for a
finished build:

    python tools/third_party.py <dist-folder>

Licence texts that no installed package carries live in tools/licenses/:
  intel-simplified-software-license.txt   from the mkl 2025.3.0 wheel on PyPI
  intel-eula-developer-tools.txt          from the intel-openmp 2025.3.0 wheel
  intel-openmp-third-party-programs.txt   from the intel-openmp 2025.3.0 wheel
  nvidia-cuda-toolkit-eula.txt            License.txt of the nvidia-cublas-cu12
                                          12.9.2.10 wheel (the CUDA Toolkit EULA)
  nvidia-cccl-2.7.0-license.txt           github.com/NVIDIA/cccl, tag v2.7.0
  spdlog-license.txt                      github.com/gabime/spdlog, branch v1.x
  libcurl-impersonate-licenses.txt        COPYING/LICENSE of curl, BoringSSL,
                                          nghttp2, nghttp3, ngtcp2, brotli, zstd
                                          and zlib, from their repositories
CTranslate2 4.8 is built against oneAPI 2025.3 (MKL statically linked into
ctranslate2.dll, libiomp5md.dll copied beside it), which is why those match.
Its ctranslate2.dll also has NVIDIA's CUDA runtime (cudart_static), Thrust
2.7 and spdlog compiled in; curl_cffi's libcurl-impersonate DLL has the
libraries listed above linked in. Both were found by the strings in the DLLs
(version strings, "CUDA driver version is insufficient for CUDA runtime
version", THRUST_200700), so re-check them when upgrading either package.
"""
from __future__ import annotations

import ast
import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSES = Path(__file__).resolve().parent / "licenses"

# Distributions that are build tools, never part of the running app.
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
              "pywin32-ctypes", "pip", "setuptools", "wheel", "pytest", "pluggy",
              "iniconfig", "pygments"}
# Kept out of the frozen build on purpose (MediaToolkit.spec). Listed here so a
# stray top-level name can never add their GPL text to the notices by mistake.
NOT_SHIPPED = {"av", "mutagen", "nvidia-cublas-cu12", "nvidia-cudnn-cu12",
               "nvidia-cuda-nvrtc-cu12"}

MIT_TEMPLATE = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

# Wheels that carry no licence file in their metadata.
EXTRA_TEXTS = {
    "ctranslate2": "MIT License\n\nCopyright (c) 2019 The OpenNMT Authors.\n\n" + MIT_TEMPLATE,
    "faster-whisper": "MIT License\n\nCopyright (c) 2023 SYSTRAN\n\n" + MIT_TEMPLATE,
}


def _text(name: str) -> str:
    return (LICENSES / name).read_text("utf-8").strip()


def assets_literal(name: str):
    """A constant from app/assets.py, read from its source. Importing the app
    at build time would create a data folder as a side effect."""
    tree = ast.parse((ROOT / "app" / "assets.py").read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


def native_components(present: set[str] | None = None) -> list[dict]:
    """Components inside the build that are not a Python distribution of
    their own, with their full terms. `present` holds the normalised names of
    the distributions in the build; an entry whose carrier ("in") is not among
    them is left out. None keeps everything."""
    entries = [
        {
            "name": "NVIDIA CUDA runtime (cudart_static, compiled into "
                    "ctranslate2/ctranslate2.dll)",
            "in": "ctranslate2",
            "license": "NVIDIA CUDA Toolkit End User License Agreement (redistributable)",
            "upstream": "https://docs.nvidia.com/cuda/eula/index.html",
            "text": "Copyright (c) NVIDIA Corporation. All rights reserved.\n\n"
                    "CTranslate2's Windows build links NVIDIA's CUDA runtime library into\n"
                    "ctranslate2.dll; it is listed as distributable in Attachment A of the\n"
                    "agreement below and is redistributed here unmodified, as part of\n"
                    "CTranslate2. It is NVIDIA's proprietary software, not open source, and\n"
                    "none of Media Toolkit's own licence applies to it. As its end user you\n"
                    "may not reverse engineer, decompile or disassemble it or remove its\n"
                    "notices (section 1.2), and NVIDIA provides it without warranty. No other\n"
                    "NVIDIA library is included: the GPU support that the app downloads on\n"
                    "request (cuBLAS) is fetched from NVIDIA's own package.\n\n"
                    "(Agreement text as published in NVIDIA's nvidia-cublas-cu12 12.9.2.10\n"
                    "package.)\n\n"
                    + _text("nvidia-cuda-toolkit-eula.txt"),
        },
        {
            "name": "NVIDIA CCCL 2.7: Thrust, CUB and libcu++ (compiled into "
                    "ctranslate2/ctranslate2.dll)",
            "in": "ctranslate2",
            "license": "Apache-2.0 WITH LLVM-exception AND BSD-3-Clause",
            "upstream": "https://github.com/NVIDIA/cccl/tree/v2.7.0",
            "text": _text("nvidia-cccl-2.7.0-license.txt"),
        },
        {
            "name": "spdlog (compiled into ctranslate2/ctranslate2.dll)",
            "in": "ctranslate2",
            "license": "MIT",
            "upstream": "https://github.com/gabime/spdlog",
            "text": _text("spdlog-license.txt"),
        },
        {
            "name": "libcurl-impersonate (curl_cffi.libs/libcurl-impersonate-*.dll): curl "
                    "8.21.0 with BoringSSL, nghttp2, nghttp3, ngtcp2, Brotli, Zstandard "
                    "and zlib 1.3.1 linked in",
            "in": "curl-cffi",
            "license": "curl AND OpenSSL AND ISC AND MIT AND BSD-3-Clause AND Zlib",
            "upstream": "https://github.com/lexiforest/curl-impersonate",
            "text": _text("libcurl-impersonate-licenses.txt"),
        },
        {
            "name": "Rust crates compiled into pydantic-core, tokenizers, hf-xet and watchfiles",
            "in": "pydantic-core",
            "license": "MIT OR Apache-2.0 (most crates); see each project's Cargo.lock",
            "upstream": "https://crates.io/",
            "text": "These four packages are written partly in Rust and link the crates\n"
                    "their Cargo.lock files name at the versions listed above (for example\n"
                    "serde, regex, onig/Oniguruma, tokio, reqwest and rustls). Almost all\n"
                    "are licensed MIT or Apache-2.0, whose texts appear above; the rest use\n"
                    "BSD-2-Clause, BSD-3-Clause, ISC, Zlib or Unicode-3.0 terms. Each\n"
                    "crate's own copyright notice is in its source on crates.io.",
        },
        {
            "name": "Silero VAD (model file shipped inside faster-whisper)",
            "in": "faster-whisper",
            "license": "MIT",
            "upstream": "https://github.com/snakers4/silero-vad",
            "text": "MIT License\n\nCopyright (c) 2020-present Silero Team\n\n" + MIT_TEMPLATE,
        },
        {
            "name": "Intel oneAPI Math Kernel Library (statically linked into "
                    "ctranslate2/ctranslate2.dll)",
            "in": "ctranslate2",
            "license": "Intel Simplified Software License",
            "upstream": "https://www.intel.com/content/www/us/en/developer/tools/oneapi/onemkl.html",
            "text": "Copyright (C) Intel Corporation. All rights reserved.\n\n"
                    + _text("intel-simplified-software-license.txt"),
        },
        {
            "name": "Intel OpenMP runtime (ctranslate2/libiomp5md.dll, redistributed "
                    "unmodified as part of CTranslate2's Windows wheel)",
            "in": "ctranslate2",
            "license": "Intel End User License Agreement for Developer Tools",
            "upstream": "https://www.intel.com/content/www/us/en/developer/articles/license/"
                        "end-user-license-agreement.html",
            "text": "Copyright (C) 1997-2025 Intel Corporation. All rights reserved.\n\n"
                    "This library is a Redistributable under the agreement below. As its end\n"
                    "user you may not reverse engineer, decompile or disassemble it, and the\n"
                    "agreement's disclaimer and limitation of liability apply to you.\n\n"
                    + _text("intel-eula-developer-tools.txt")
                    + "\n\n--- third-party-programs.txt (Intel OpenMP) ---\n"
                    + _text("intel-openmp-third-party-programs.txt"),
        },
        {
            "name": "oneDNN 3.1.1 (statically linked into ctranslate2/ctranslate2.dll)",
            "in": "ctranslate2",
            "license": "Apache-2.0",
            "upstream": "https://github.com/uxlfoundation/oneDNN",
            "text": "Copyright 2016-2023 Intel Corporation\n\n"
                    "Licensed under the Apache License, Version 2.0 (full text in the\n"
                    "Apache-2.0 entries above). oneDNN's own third-party notices are at\n"
                    "https://github.com/uxlfoundation/oneDNN/blob/v3.1.1/THIRD-PARTY-PROGRAMS",
        },
        {
            "name": "Microsoft Visual C++ runtime (vcruntime140.dll, vcruntime140_1.dll, "
                    "msvcp140*.dll and copies inside package folders)",
            "license": "Microsoft Visual Studio Distributable Code",
            "upstream": "https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files",
            "text": "Copyright (c) Microsoft Corporation.\n\n"
                    "These files are Distributable Code under the Microsoft Visual Studio\n"
                    "license terms. They may be used only on Microsoft Windows; you may not\n"
                    "alter Microsoft's copyright, trademark or patent notices in them or use\n"
                    "them in malicious, deceptive or unlawful programs. Microsoft's\n"
                    "trademarks may not be used to suggest this program comes from or is\n"
                    "endorsed by Microsoft.",
        },
    ]
    if present is None:
        return entries
    return [e for e in entries if e.get("in") is None or _norm(e["in"]) in present]


def runtime_downloads_text() -> str:
    """What the app can download later at the user's request. Not part of the
    installer, so no redistribution terms apply, but users should know."""
    try:
        wheel, lic = assets_literal("CUBLAS_WHEEL"), assets_literal("NVIDIA_LICENSE")
        cublas = f"{wheel['package']} {wheel['version']} (sha256 {wheel['sha256']})"
        nv_url = lic["url"]
    except Exception:
        cublas, nv_url = "nvidia-cublas-cu12", "https://docs.nvidia.com/cuda/eula/index.html"
    return (
        "DOWNLOADED LATER, ONLY IF YOU ASK FOR IT\n"
        "----------------------------------------\n"
        "These are not part of the installer. The app fetches them from their\n"
        "publishers when you use the feature, and their own terms apply:\n\n"
        f"* GPU support: NVIDIA cuBLAS from NVIDIA's package on PyPI, {cublas}.\n"
        "  Proprietary NVIDIA software under the NVIDIA Software License Agreement\n"
        f"  ({nv_url}). The app shows this before downloading and saves NVIDIA's\n"
        "  licence text next to the libraries (runtime/cuda/NVIDIA-LICENSE.txt).\n"
        "* Speech models: Whisper models converted for CTranslate2, downloaded from\n"
        "  Hugging Face (huggingface.co, whose terms of service apply). The original\n"
        "  Whisper weights are by OpenAI (MIT License); the conversions are by\n"
        "  SYSTRAN, Mobius Labs and the distil-whisper authors (MIT License).\n"
        "* Site support updates: newer yt-dlp and yt-dlp-ejs from PyPI (Unlicense;\n"
        "  yt-dlp-ejs bundles meriyah, ISC, and astring, MIT).\n"
        "* ffmpeg repair: the same pinned FFmpeg build described above, from\n"
        "  github.com/yt-dlp/FFmpeg-Builds.\n"
        "* SponsorBlock (only when you tick it): segment data from sponsor.ajay.app,\n"
        "  licensed CC BY-NC-SA 4.0 (https://sponsor.ajay.app/, attribution to the\n"
        "  SponsorBlock contributors; non-commercial use).\n"
    )


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def distributions_for(top_levels: set[str]) -> list[md.Distribution]:
    """Map top-level import names (and native-library folder names) to the
    installed distributions that provide them."""
    owners = md.packages_distributions()
    names: set[str] = set()
    for top in top_levels:
        for dist in owners.get(top, []):
            names.add(_norm(dist))
        # Wheels that vendor DLLs into "<pkg>.libs" folders.
        if top.endswith(".libs"):
            for dist in owners.get(top[:-5], []):
                names.add(_norm(dist))
    out = []
    for name in sorted(names - BUILD_ONLY - NOT_SHIPPED):
        try:
            out.append(md.distribution(name))
        except md.PackageNotFoundError:
            continue
    return out


def _license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    texts = []
    for f in dist.files or []:
        low = str(f).lower().replace("\\", "/")
        if f.suffix in (".py", ".pyc", ".pyi") or "__pycache__" in low:
            continue
        base = low.rsplit("/", 1)[-1]
        # onnxruntime's Privacy.md is a notice its licence asks apps to pass on.
        if any(k in base for k in ("licen", "copying", "notice", "authors", "privacy")) \
                or "/licenses/" in low:
            try:
                body = Path(dist.locate_file(f)).read_text("utf-8", errors="replace").strip()
            except OSError:
                continue
            if body:
                texts.append((str(f), body))
    return texts


def _license_id(dist: md.Distribution) -> str:
    m = dist.metadata
    expr = m.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in (m.get_all("Classifier") or [])
                   if c.startswith("License")]
    if classifiers:
        return "; ".join(classifiers)
    raw = (m.get("License") or "").strip().splitlines()
    return raw[0][:80] if raw else "see text"


def _homepage(dist: md.Distribution) -> str:
    m = dist.metadata
    for entry in m.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("source", "repository", "source code", "homepage", "code"):
            return url.strip()
    return (m.get("Home-page") or "").strip() or f"https://pypi.org/project/{m['Name']}/"


def ffmpeg_section(bin_dir: Path) -> str:
    exe = bin_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    banner = ""
    if exe.exists():
        try:
            out = subprocess.run([str(exe), "-hide_banner", "-version"], capture_output=True,
                                 text=True, timeout=30).stdout
            banner = out.strip()
        except Exception:
            banner = ""
    pinned = bin_dir / "FFMPEG-VERSION.txt"
    pin_text = pinned.read_text("utf-8").strip() if pinned.exists() else ""
    lic = bin_dir / "FFMPEG-LICENSE.txt"
    body = lic.read_text("utf-8", errors="replace") if lic.exists() else \
        "GNU General Public License, version 3: https://www.gnu.org/licenses/gpl-3.0.txt"
    return (
        "FFmpeg (bin/ffmpeg.exe, bin/ffprobe.exe)\n"
        "License: GPL-3.0-or-later (this build enables GPL components such as x264 and x265)\n"
        "It is a separate program that Media Toolkit starts as its own process; it is\n"
        "not linked into Media Toolkit. The build is the static build published by the\n"
        "yt-dlp project, redistributed unmodified.\n\n"
        + (pin_text + "\n\n" if pin_text else "")
        + "Corresponding source: the FFmpeg source at the commit named above, and the\n"
        "FFmpeg-Builds scripts at the commit named above, which pin the exact version\n"
        "of every library linked into the build. On request, the maintainer of Media\n"
        "Toolkit will also provide a copy of the complete corresponding source, for at\n"
        "least three years after the last release that included this build; open an\n"
        "issue at https://github.com/AnotherAH/media-toolkit/issues.\n\n"
        + (banner + "\n\n" if banner else "") + body.strip()
    )


def render(dists: list[md.Distribution], bin_dir: Path | None) -> str:
    apache_text = ""
    for d in dists:
        if "apache" in _license_id(d).lower():
            for _, body in _license_texts(d):
                if "Apache License" in body and "Version 2.0" in body:
                    apache_text = body
                    break
        if apache_text:
            break
    native = native_components({_norm(d.metadata['Name']) for d in dists})

    parts = [
        "THIRD-PARTY NOTICES",
        "===================",
        "",
        "Media Toolkit is released under the MIT License (see LICENSE.txt). The",
        "installed program also contains the third-party software listed below, each",
        "under its own license, reproduced in full further down. FFmpeg is licensed",
        "under the GNU GPL and is included as a separate program; where to get its",
        "source code is given in its entry. The Intel runtime libraries and the",
        "NVIDIA CUDA runtime inside ctranslate2.dll are proprietary: you may not",
        "reverse engineer, decompile or disassemble them.",
        "",
        "Summary",
        "-------",
    ]
    rows = [(d.metadata["Name"], d.version, _license_id(d)) for d in dists]
    rows.append(("Python", sys.version.split()[0], "PSF-2.0"))
    if bin_dir:
        rows.append(("FFmpeg", "see entry", "GPL-3.0-or-later"))
    rows += [(n["name"].split(" (")[0], "-", n["license"]) for n in native]
    # A few native entries have long names; those get a line of their own
    # rather than pushing every other row off the right edge.
    width = min(max(len(r[0]) for r in rows), 40) + 2
    for name, ver, lic in rows:
        if len(name) > width - 2:
            parts += [name, f"{'':<{width}}{ver:<14}{lic}"]
        else:
            parts.append(f"{name:<{width}}{ver:<14}{lic}")
    parts += ["", runtime_downloads_text()]

    sep = "\n\n" + "=" * 78 + "\n"
    blocks = []
    for d in dists:
        name = d.metadata["Name"]
        head = f"{name} {d.version}\nLicense: {_license_id(d)}\nSource: {_homepage(d)}\n"
        texts = _license_texts(d)
        extra = EXTRA_TEXTS.get(_norm(name))
        if extra and not any("licen" in f.lower() for f, _ in texts):
            texts.insert(0, ("LICENSE", extra))
        if not texts and "apache" in _license_id(d).lower() and apache_text:
            texts = [("LICENSE (Apache-2.0)", apache_text)]
        body = "\n\n".join(f"--- {fname} ---\n{text}" for fname, text in texts) or \
            "(the package ships no license file; see the source address above)"
        blocks.append(head + "\n" + body)

    py_license = Path(sys.base_prefix) / "LICENSE.txt"
    blocks.append(f"Python {sys.version.split()[0]} runtime (python3*.dll and the standard library)\n"
                  "License: PSF-2.0 (includes notices for OpenSSL, libffi, SQLite, zlib, bzip2, xz, "
                  "mpdecimal and the Microsoft Distributable Code terms of the Windows build)\n\n"
                  + (py_license.read_text("utf-8", errors="replace").strip()
                     if py_license.exists() else "https://docs.python.org/3/license.html"))
    if bin_dir:
        blocks.append(ffmpeg_section(bin_dir))
    for n in native:
        blocks.append(f"{n['name']}\nLicense: {n['license']}\nSource: {n['upstream']}\n\n{n['text']}")

    return "\n".join(parts) + sep + sep.join(blocks) + "\n"


def top_levels_from_toc(*tocs) -> set[str]:
    """Top-level names from PyInstaller TOC entries (dest_name, src, typecode)."""
    tops: set[str] = set()
    for toc in tocs:
        for entry in toc:
            dest = str(entry[0]).replace("\\", "/")
            if entry[2] in ("PYMODULE", "PYSOURCE"):
                tops.add(dest.split(".")[0])
            elif "/" in dest:
                tops.add(dest.split("/")[0])
            else:
                tops.add(dest.split(".")[0])
    return tops


def top_levels_from_dist(dist_dir: Path) -> set[str]:
    """For a finished build: folder names in _internal, plus every module name
    inside the PYZ archive embedded in the executable."""
    internal = dist_dir / "_internal"
    tops = {p.name if p.is_dir() else p.name.split(".")[0] for p in internal.iterdir()} \
        if internal.is_dir() else set()
    from PyInstaller.archive.readers import CArchiveReader
    exe = next(dist_dir.glob("*.exe"))
    pkg = CArchiveReader(str(exe))
    for name in pkg.toc:
        if name.endswith(".pyz"):
            tops |= {n.split(".")[0] for n in pkg.open_embedded_archive(name).toc}
    return tops


def write(dest: Path, top_levels: set[str], bin_dir: Path | None) -> Path:
    text = render(distributions_for(top_levels), bin_dir)
    dest.write_text(text, encoding="utf-8")
    return dest


if __name__ == "__main__":
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "MediaToolkit"
    tops = top_levels_from_dist(dist)
    out = write(dist / "THIRD-PARTY-NOTICES.txt", tops,
                dist / "bin" if (dist / "bin").is_dir() else ROOT / "bin")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
