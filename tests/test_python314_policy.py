"""Enforce the 3.14 annotation policy and exercise its real runtime boundaries."""

# Check both declarations and runtime consumers: source syntax alone cannot prove
# that dataclasses, diagnostics and Qt slots work with native deferred annotations.


import ast
import sys
import sysconfig
import tomllib
from dataclasses import replace
from pathlib import Path
from threading import get_ident

from PySide6.QtCore import Qt

from metadata_polisher.domain.metadata import MetadataSnapshot, Position
from metadata_polisher.execution.cancellation import CancellationToken
from metadata_polisher.execution.events import OperationEventSink, OperationStageChanged
from metadata_polisher.execution.executor import SerialBackgroundExecutor
from metadata_polisher.infrastructure.diagnostics import sanitise_diagnostic_data
from metadata_polisher.session.state import GroupSelection, SessionState
from metadata_polisher.ui.qt_bridge import QtOperationBridge
from metadata_polisher.ui.quick.backend import QuickBackend
from tests.ui.helpers import make_group

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY_ROOTS = ("src", "tests", "scripts")


def test_first_party_python_uses_native_deferred_annotations() -> None:
    violations: list[str] = []

    # AST nodes distinguish an actual import from documentation examples and
    # embedded legacy fixtures. The explicit roots exclude .venv and builds.
    for root in FIRST_PARTY_ROOTS:
        for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "__future__"
                    and any(alias.name == "annotations" for alias in node.names)
                ):
                    violations.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{node.lineno}")

    assert violations == [], "Remove audited first-party future-annotations imports:\n" + "\n".join(violations)


def test_python_runtime_and_declared_tool_targets_agree() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        configuration = tomllib.load(stream)

    assert sys.version_info[:2] == (3, 14)
    assert sys.implementation.name == "cpython"
    assert not sysconfig.get_config_var("Py_GIL_DISABLED")
    assert configuration["project"]["requires-python"] == ">=3.14"
    assert configuration["tool"]["ruff"]["target-version"] == "py314"
    assert configuration["tool"]["mypy"]["python_version"] == "3.14"


def test_dataclass_creation_replacement_and_diagnostic_field_walk() -> None:
    original = MetadataSnapshot(title="序章 / Overture", composers=("Composer A",), track=Position(2, 12))
    updated = replace(original, title="新しい題名 / New title", composers=("Composer A", "Composer B"))

    # Diagnostics is the actual dataclass-field consumer. It reads names and
    # values, without requiring every type-only controller name to resolve.
    rendered = sanitise_diagnostic_data(updated)

    assert isinstance(rendered, dict)
    assert rendered["title"] == "新しい題名 / New title"
    assert rendered["composers"] == ["Composer A", "Composer B"]
    assert rendered["track"] == {"number": 2, "total": 12}
    assert original.title == "序章 / Overture"
    assert original.composers == ("Composer A",)


def test_populated_qt_model_and_decorated_worker_slots_with_native_annotations(qtbot) -> None:
    group = make_group("annotation-group", "annotation-file", "日本語 / Long Latin title")
    backend = QuickBackend(
        state=SessionState(root=Path("library"), groups=(group,), selection=GroupSelection("annotation-group")),
    )

    assert backend.files.data(backend.files.index(0, 0), Qt.ItemDataRole.UserRole) == "annotation-file"

    ui_thread = get_ident()
    received_threads: list[int] = []
    received_stages: list[OperationStageChanged] = []
    executor = SerialBackgroundExecutor()
    bridge = QtOperationBridge(executor, backend)

    def observe_event(event: object) -> None:
        received_threads.append(get_ident())

        if isinstance(event, OperationStageChanged):
            received_stages.append(event)

    bridge.operation_event.connect(observe_event)

    def work(cancellation: CancellationToken, events: OperationEventSink) -> MetadataSnapshot:
        cancellation.raise_if_cancelled()
        events.emit(OperationStageChanged("ANNOTATIONS-0001", "building_review"))
        return MetadataSnapshot(title="Worker result", track=Position(1, 1))

    try:
        with qtbot.waitSignal(bridge.completed, timeout=3000) as completed:
            bridge.submit("ANNOTATIONS-0001", work)

        qtbot.waitUntil(lambda: bool(received_stages), timeout=3000)
        assert completed.args == ["ANNOTATIONS-0001", MetadataSnapshot(title="Worker result", track=Position(1, 1))]
        assert received_stages == [OperationStageChanged("ANNOTATIONS-0001", "building_review")]
        assert received_threads and all(thread == ui_thread for thread in received_threads)
    finally:
        executor.shutdown()
        backend.shutdown()
