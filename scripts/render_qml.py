"""Render populated QML windows using synthetic metadata and isolated settings."""

import argparse
import os
from pathlib import Path
from typing import cast


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/qml-preview"))
    parser.add_argument("--style", choices=("native", "fusion"), default="fusion")
    parser.add_argument("--platform", choices=("offscreen", "windows"), default="offscreen")
    parser.add_argument("--font", type=int, default=9, help="Segoe UI size in points; use 14 to inspect larger text.")
    parser.add_argument("--width", type=int, default=1180)
    parser.add_argument("--height", type=int, default=820)
    arguments = parser.parse_args()

    if arguments.style == "native" and arguments.platform == "offscreen":
        parser.error("Native Windows controls need --platform windows; use Fusion for offscreen checks.")

    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = (
        "offscreen:size=1920x1080" if arguments.platform == "offscreen" else "windows"
    )

    if arguments.platform == "offscreen":
        os.environ["QT_QUICK_BACKEND"] = "software"
    else:
        os.environ.pop("QT_QUICK_BACKEND", None)

    os.environ.pop("QSG_RHI_BACKEND", None)

    if arguments.style == "fusion":
        os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"
    else:
        os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)

    from PySide6.QtCore import QCoreApplication, QEvent, QPointF
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
    from PySide6.QtQuick import QQuickItem, QQuickWindow
    from PySide6.QtTest import QTest

    from metadata_polisher.domain.media import LocalMediaFile, MediaReadResult, StreamInfo
    from metadata_polisher.domain.metadata import FieldReadState, MetadataField, MetadataSnapshot, Position
    from metadata_polisher.scanner.grouping import AlbumGroup, GroupingReason
    from metadata_polisher.session.state import GroupSelection, GroupState, SessionState
    from metadata_polisher.ui.quick.application import create_quick_engine
    from metadata_polisher.ui.quick.apply import QuickApply
    from metadata_polisher.ui.quick.backend import QuickBackend
    from metadata_polisher.ui.quick.settings import QuickSettings

    application = QGuiApplication([])
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"

    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))

    application.setFont(QFont("Segoe UI", arguments.font))
    application.setQuitOnLastWindowClosed(False)
    groups = []

    # The paths are invented and no scan, provider or write is submitted. Use
    # enough rows to expose both scrollbar directions and recycled delegates.
    for album_number, album in enumerate(("The Lighthouse — Original Soundtrack", "Evening Sessions"), 1):
        sources = []

        for track_number in range(1, 41):
            title = f"Movement {track_number:02d} — A synthetic long title for layout review"
            metadata = MetadataSnapshot(
                title=title, artists=("Sample Ensemble",), album=album,
                album_artists=("Sample Ensemble",), composers=("A. Composer",),
                track=Position(track_number, 40), disc=Position(1, 1), date="2026", genres=("Soundtrack",),
            )
            sources.append(LocalMediaFile(
                path=Path("Synthetic library") / album / f"{track_number:02d}.flac",
                format_id="flac", file_id=f"album-{album_number}-track-{track_number}",
                read_result=MediaReadResult(
                    metadata=metadata,
                    field_states={field: FieldReadState.PRESENT for field in MetadataField},
                    stream_info=StreamInfo(180.0, 48_000, 2, 24, "FLAC"),
                ),
            ))

        groups.append(GroupState(group=AlbumGroup(
            group_id=f"album-{album_number}", files=tuple(sources), album_title=album,
            reason=GroupingReason.DIRECTORY_ALBUM_CONSISTENT,
        )))

    state = SessionState(root=Path("Synthetic library"), groups=tuple(groups), selection=GroupSelection("album-1"))
    backend = QuickBackend(state=state)
    warnings: list[str] = []
    engine = create_quick_engine(backend, warnings=warnings)
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow)

    try:
        window.resize(arguments.width, arguments.height)
        backend.selectFile("album-1-track-1", False)
        backend.selectField("title")
        backend.setIncluded("album-1-track-1", True)
        backend.openReview()
        review = window.findChild(QQuickWindow, "metadataReviewWindow")
        assert review is not None
        review.resize(min(1080, arguments.width), arguments.height)
        QTest.qWait(250)

        for name, rendered_window in (("main", window), ("review", review)):
            image = rendered_window.grabWindow()
            assert not image.isNull(), name
            assert image.save(str(output / f"{name}.png")), name

        backend.closeReview()
        settings_ui = cast(QuickSettings, backend.settingsUi)
        settings_ui.open()
        settings = window.findChild(QQuickWindow, "settingsWindow")
        assert settings is not None
        settings.resize(min(760, arguments.width), min(490, arguments.height))
        QTest.qWait(150)
        assert settings.grabWindow().save(str(output / "settings.png"))

        # Keep a second view of the list page: its viewport and footer use
        # different surfaces from the form controls on the default tab.
        tab_bar = settings.findChild(QQuickItem, "settingsTabs")
        assert tab_bar is not None
        tab_bar.setProperty("currentIndex", 4)
        QTest.qWait(60)
        for name in ("saveSettingsButton", "cancelSettingsButton"):
            button = settings.findChild(QQuickItem, name)
            assert button is not None and button.isVisible(), name
            bottom = button.mapToScene(QPointF(0, button.height())).y()
            assert bottom <= settings.height(), f"{name} fell below the Settings window"
        assert settings.grabWindow().save(str(output / "settings-external.png"))
        settings_ui.reject()

        # A single selected synthetic file exposes whether a short rename
        # summary wastes space that should belong to the file table.
        apply_ui = cast(QuickApply, backend.applyUi)
        assert apply_ui.beginRename()
        rename = window.findChild(QQuickWindow, "renameFilesWindow")
        assert rename is not None
        rename.resize(min(1050, arguments.width), min(600, arguments.height))
        QTest.qWait(150)
        assert rename.grabWindow().save(str(output / "rename.png"))
        apply_ui.cancelRename()
    finally:
        backend.shutdown()

        for child in window.findChildren(QQuickWindow):
            child.hide()

        window.hide()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert not warnings, "\n".join(warnings)
    print(f"Rendered synthetic QML windows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
