"""Run Qt tests on a synthetic desktop without sending input to the real desktop."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    config = Path("build/offscreen-test-screen.json")
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "screens": [
                    {
                        "name": "Test display",
                        "width": 1920,
                        "height": 1080,
                        "logicalDpi": 96,
                        "logicalBaseDpi": 96,
                        "dpr": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # A relative platform argument also avoids treating a Windows drive colon
    # as a separator in Qt's offscreen plugin arguments.
    os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={config.as_posix()}"
    os.environ["QT_QUICK_BACKEND"] = "software"
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"

    import pytest
    from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication

    application = QGuiApplication([])
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 9))
    application.setQuitOnLastWindowClosed(False)
    return pytest.main(sys.argv[1:] or ["-q"])


if __name__ == "__main__":
    raise SystemExit(main())
