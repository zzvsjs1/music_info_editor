"""Named catalogue identities with the existing tuple comparison contract."""

from typing import NamedTuple


class ReleaseMediumIdentity(NamedTuple):
    """Identify a medium without confusing its catalogue and provider fields.

    The field order deliberately matches the existing public tuple shape. This
    keeps captured selections, dictionary keys, sorting and JSON projections
    compatible while business logic can read each component by name.
    """

    engine_id: str
    source_id: str
    release_id: str
    medium_index: int

    @property
    def release_identity(self) -> tuple[str, str, str]:
        """Identify the catalogue release independently of its selected medium."""
        return self.engine_id, self.source_id, self.release_id
