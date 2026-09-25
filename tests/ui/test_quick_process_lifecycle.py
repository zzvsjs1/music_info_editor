"""A closed desktop application must release its process and portable executable."""

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("workflow", ("idle", "scan", "apply"))
def test_closing_the_main_window_exits_the_real_runtime(tmp_path, workflow):
    script = textwrap.dedent('''
        import faulthandler
        import sys
        import wave
        from pathlib import Path
        from PySide6.QtCore import QTimer
        from metadata_polisher.ui.quick import application

        faulthandler.dump_traceback_later(6)
        workflow = sys.argv[1]
        media = Path.cwd() / "library"
        media.mkdir()
        with wave.open(str(media / "01.wav"), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\\0\\0" * 800)

        original = application.create_quick_engine
        phase = "start"

        def create(backend):
            engine = original(backend)
            window = engine.rootObjects()[0]
            timer = QTimer(engine)
            timer.setInterval(30)

            def advance():
                global phase
                if backend.busy:
                    return

                if phase == "start" and workflow != "idle":
                    phase = "scanned"
                    assert backend.scan(str(media), False)
                    return

                if phase == "scanned" and workflow == "apply":
                    phase = "written"
                    file_id = backend.session_state.groups[0].group.files[0].file_id
                    backend.selectFile(file_id, False)
                    backend.selectField("title")
                    assert backend.beginEdit()
                    assert backend.commitEdit("Lifecycle test")
                    backend.setIncluded(file_id, True)
                    assert backend.applyUi.beginApply()
                    assert backend.applyUi.confirmApply()
                    return

                # Results may still be open when the user closes the main
                # window. They must not keep the event loop or process alive.
                timer.stop()
                window.close()

            timer.timeout.connect(advance)
            timer.start()
            return engine

        application.create_quick_engine = create
        result = application.run_quick(["metadata-polisher"])
        print("Runtime returned", result, flush=True)
        raise SystemExit(result)
    ''')
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, workflow],
            cwd=tmp_path,
            env={
                **os.environ,
                "QT_QPA_PLATFORM": os.environ.get(
                    "QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen",
                ).split(":", 1)[0],
            },
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"The closed application did not exit.\n{error.stdout!r}\n{error.stderr!r}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Runtime returned 0" in result.stdout
