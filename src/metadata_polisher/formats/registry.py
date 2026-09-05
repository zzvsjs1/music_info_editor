"""Deterministic format adapter selection by extension and content probe."""

import logging
from collections.abc import Iterable
from pathlib import Path

from metadata_polisher.formats.base import MediaFormatAdapter
from metadata_polisher.formats.flac import FlacAdapter
from metadata_polisher.formats.mp3 import Mp3Adapter
from metadata_polisher.formats.mp4 import Mp4Adapter
from metadata_polisher.formats.tak import TakAdapter
from metadata_polisher.formats.wave import WaveAdapter

LOGGER = logging.getLogger(__name__)


class FormatRegistry:
    """Select the first registered adapter that confirms it can handle a file."""

    def __init__(self, adapters: Iterable[MediaFormatAdapter] | None = None) -> None:
        if adapters is None:
            adapters = (
                FlacAdapter(),
                Mp3Adapter(),
                Mp4Adapter(),
                WaveAdapter(),
                TakAdapter(),
            )

        self._adapters = tuple(adapters)
        self._supported_extensions = frozenset(
            extension.casefold()
            for adapter in self._adapters
            for extension in adapter.extensions
        )

    @property
    def supported_extensions(self) -> frozenset[str]:
        """Normalised extensions owned by an installed adapter."""
        return self._supported_extensions

    def detect(self, path: Path) -> MediaFormatAdapter | None:
        """Probe matching-extension adapters in stable registration order."""
        extension = path.suffix.casefold()

        # The suffix narrows the candidates; a content probe still has to
        # confirm the format, because filenames can be misleading or corrupt.
        for adapter in self._adapters:
            supported_extensions = frozenset(candidate.casefold() for candidate in adapter.extensions)

            if extension not in supported_extensions:
                continue

            try:
                if adapter.can_handle(path):
                    return adapter
            except Exception:
                # Third-party parser probes can fail on a corrupt or misleadingly
                # named file. Keep other candidate adapters usable and preserve the
                # exception context in diagnostics rather than swallowing it silently.
                LOGGER.warning(
                    "Format probe failed for adapter %s and path %s",
                    adapter.format_id,
                    path,
                    exc_info=True,
                )

        return None
