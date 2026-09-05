"""Read-only Qt models projected from immutable session state."""

from metadata_polisher.ui.models.diff_model import MetadataDiffModel
from metadata_polisher.ui.models.file_table_model import FileTableModel
from metadata_polisher.ui.models.group_model import GroupListModel

# These are presentation adapters: callers replace their immutable input state,
# while review decisions and validation remain in the application/session layers.
__all__ = ["FileTableModel", "GroupListModel", "MetadataDiffModel"]
