"""Shared test setup.

Every test run gets its own empty data folder. MEDIA_TOOLKIT_HOME is set here,
at conftest import, which pytest does before it imports any test module and
therefore before anything imports app.config (which fixes DATA_ROOT at
import). Tests can never read or write the real settings, models or
downloads, even when they run on a developer's machine.

Tests that need the internet are marked @pytest.mark.network and skipped
unless pytest is run with --network.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="mt-tests-"))
os.environ["MEDIA_TOOLKIT_HOME"] = str(_HOME)
# Nothing a test does should reach a real Hugging Face account or cache.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HOME", str(_HOME / "hf"))

if "app.config" in sys.modules:                      # pragma: no cover
    raise RuntimeError("app.config was imported before tests/conftest.py set MEDIA_TOOLKIT_HOME")


def pytest_addoption(parser):
    parser.addoption("--network", action="store_true", default=False,
                     help="also run tests marked 'network' (they use the internet)")


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs the internet; skipped unless --network")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="needs the internet; run with --network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def pytest_unconfigure(config):
    shutil.rmtree(_HOME, ignore_errors=True)


@pytest.fixture(scope="session")
def data_root() -> Path:
    """The data folder app.config uses for this test session."""
    return _HOME
