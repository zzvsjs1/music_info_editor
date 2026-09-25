"""The installed application has one frontend and no Widgets import path."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("arguments", [[], ["--qml"]])
def test_default_and_compatibility_launch_use_the_quick_runtime(monkeypatch, arguments):
    from metadata_polisher import __main__ as entry
    from metadata_polisher.ui.quick import application

    observed = []
    argv = ["metadata-polisher", *arguments]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(application, "run_quick", lambda values: observed.append(values) or 17)

    # Fail explicitly instead of opening the old event loop on the red run.
    if hasattr(entry, "create_application"):
        monkeypatch.setattr(entry, "create_application", lambda _: pytest.fail("Legacy Widgets frontend started"))

    assert entry.main() == 17
    assert observed == [["metadata-polisher"]]


def test_importing_the_entrypoint_does_not_load_widgets(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import json, sys; import metadata_polisher.__main__; "
         "print(json.dumps([name for name in sys.modules if name == 'PySide6.QtWidgets']))"],
        cwd=tmp_path,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )

    assert json.loads(result.stdout) == []


def test_application_source_has_no_widgets_frontend_imports():
    root = Path(__file__).resolve().parents[2] / "src" / "metadata_polisher"
    references = [str(path.relative_to(root)) for path in root.rglob("*.py")
                  if "PySide6.QtWidgets" in path.read_text(encoding="utf-8")]

    assert references == []
