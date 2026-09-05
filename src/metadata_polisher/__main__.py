"""Command-line entry point for the desktop application."""

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from metadata_polisher.bootstrap import create_application
from metadata_polisher.infrastructure.paths import ApplicationPaths, ensure_writable_application_dir


def main() -> int:
    """Validate portable storage before constructing logging or worker services."""
    application = QApplication.instance()

    if application is None:
        QApplication(sys.argv)

    # Keep Qt alive for the modal error, but do not create the main window or
    # runtime services until the executable's own storage has been checked.
    writable = ensure_writable_application_dir(ApplicationPaths.detect())

    if not writable.ok:
        QMessageBox.critical(None, "Metadata Polisher cannot start", writable.message or "Storage is not writable.")
        return 1

    application, window = create_application(sys.argv)
    window.show()

    # The event loop owns window interaction until shutdown; return its exit
    # status to both the console entry point and the frozen desktop launcher.
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
