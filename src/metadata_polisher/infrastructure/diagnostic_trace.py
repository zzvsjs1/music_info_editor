"""Optional structured diagnostics that cannot interrupt processing work."""

import json
import logging

from metadata_polisher.infrastructure.diagnostics import sanitise_diagnostic_data


class DetailedTraceSink:
    """Write sanitised deterministic JSON through the configured rotating logger."""

    def __init__(self, logger: logging.Logger, *, enabled: bool = False) -> None:
        self._logger = logger
        self.enabled = enabled

    def record(self, operation_id: str, event: str, details: object) -> None:
        """Record one optional diagnostic event without changing operation results."""
        if not self.enabled:
            return

        try:
            document = sanitise_diagnostic_data({
                "operation_id": operation_id,
                "event": event,
                "details": details,
            })
            # Stable keys aid comparisons between runs, while allow_nan=False
            # ensures the emitted trace remains valid JSON for diagnostic readers.
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            self._logger.debug(encoded)
        except Exception:
            # Diagnostics are supplementary: neither an unexpected value nor a
            # full/unavailable log destination may fail a metadata operation.
            # Logging this failure again could recurse through the same handler.
            return
