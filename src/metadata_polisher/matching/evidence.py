"""Small comparison-only extraction rules shared by ranking and mapping.

Read states establish which tag values are usable. Cleaning and filename
fallback supply evidence only; neither operation changes the stored metadata.
"""

from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField


def _usable_text(value: str | None) -> str | None:
    """Collapse comparison whitespace and distinguish blank from usable text."""
    if value is None:
        return None

    cleaned = " ".join(value.split())

    return cleaned or None


def effective_local_title(file: LocalMediaFile) -> str | None:
    """Prefer a usable PRESENT title, otherwise use usable filename evidence.

    A scanner can successfully read a whitespace-only tag. That is still a
    PRESENT read, but contains no title to compare. Conversely, a stale value
    beside an UNREADABLE/MISSING/UNSUPPORTED state must never become evidence.
    Both cases may use the independent filename hint without rewriting tags.
    """
    title_state = file.read_result.field_states[MetadataField.TITLE]

    # Read authority comes before text quality: stale text beside a failed or
    # absent read must not influence either release ranking or track identity.
    if title_state is FieldReadState.PRESENT:
        title = _usable_text(file.read_result.metadata.title)

        if title is not None:
            return title

    # Filename hints are independent observations. Sharing this fallback keeps
    # the ranker and mapper consistent without turning a hint into a stored tag.
    return _usable_text(file.filename_hints.probable_title)
