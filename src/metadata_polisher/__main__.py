"""Command-line entry point for the desktop application."""

import sys


def main() -> int:
    """Launch the sole Qt Quick interface after its portable-storage check."""
    from metadata_polisher.ui.quick.application import run_quick

    # Existing shortcuts may still contain the migration switch. Treat it as
    # a harmless compatibility alias; no second frontend remains selectable.
    arguments = [argument for argument in sys.argv if argument != "--qml"]
    return run_quick(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
