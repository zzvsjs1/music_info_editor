from metadata_polisher.bootstrap import create_application


def test_main_window_can_be_created(qapp, qtbot, tmp_path):
    # Isolated settings make construction exercise a clean application start
    # without depending on or updating the user's portable configuration.
    app, window = create_application([], settings_file=tmp_path / "settings.json")

    qtbot.addWidget(window)

    assert app is qapp
    assert window.windowTitle() == "Metadata Polisher"
