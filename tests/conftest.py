"""Test path bootstrap: product sources live under ``src/``."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
tests_root = Path(__file__).resolve().parent
candidates = [ROOT / "src", tests_root] + sorted(
    path for path in tests_root.iterdir()
    if path.is_dir() and not path.name.startswith((".", "_")))
for candidate in candidates:
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)


@pytest.fixture(autouse=True)
def _hermetic_native_telemetry(request, monkeypatch):
    """Keep pre-existing tests hermetic from live OS collectors.

    The daemon enables native collectors by default in production, but
    tests that script their own ActivityWatch fixtures must not observe
    the real desktop. Tests under ``test_native_*`` files exercise the
    live factory and are exempt; they inject scripted sources directly.
    """
    if "native" in Path(str(request.node.fspath)).name:
        return
    from noema.runtime.daemon import NoemaDaemon

    def _preset_only(self):
        # Honor an explicitly injected collector, never build live ones.
        return self._native_sources

    monkeypatch.setattr(
        NoemaDaemon, "_native_collector_instance", _preset_only)
