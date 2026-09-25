"""Validated grouping drafts for the Qt Quick library controls."""

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Property, QObject, Signal, Slot

from metadata_polisher.infrastructure.settings import GeneralSettings, load_settings, save_settings
from metadata_polisher.session.group_editing import (
    merge_session_groups,
    regroup_candidate_decisions,
    set_disc_override,
    split_session_group,
)
from metadata_polisher.session.state import SessionState

if TYPE_CHECKING:
    from metadata_polisher.ui.quick.backend import QuickBackend


class QuickLibrary(QObject):
    """Keep dialogue state separate from immutable library state until accepted."""

    changed = Signal()

    def __init__(self, host: QuickBackend) -> None:
        super().__init__(host)
        self._host = host
        self._disc_snapshot: SessionState | None = None
        self._disc_value = 0
        self._disc_error = ""
        loaded = load_settings(host.settings_file) if host.settings_file is not None else None
        self._settings_error = loaded.error if loaded is not None else None
        host.changed.connect(self.changed)

    def remember_root(self, root: Path) -> str:
        """Remember an accepted scan without overwriting changed settings on disk."""
        host = self._host
        previous = host.app_settings
        current = load_settings(host.settings_file) if host.settings_file is not None else None

        # The successful scan has already changed the session. Keep the folder
        # in memory even when an automatic convenience save is unsafe or fails.
        host.app_settings = replace(previous, general=GeneralSettings(str(root)))
        host.changed.emit()

        if host.settings_file is None or current is None:
            return ""

        if self._settings_error is not None:
            return "The library was scanned; corrupt settings were preserved. " + self._settings_error

        if (
            current.error is not None
            or current.settings != previous
            or any("'ui.version'" in warning for warning in current.warnings)
        ):
            # The user can explicitly save preferences later. A completed scan
            # must not replace external edits or an unsupported layout schema.
            return "The library was scanned; externally changed or unsupported settings were preserved."

        try:
            save_settings(host.settings_file, host.app_settings)
        except OSError as error:
            return f"The library was scanned, but settings could not be saved: {error}"

        return ""

    def _can_disc(self) -> bool:
        group = self._host._group()
        return group is not None and not group.requires_rescan and not self._host.busy

    def _can_split(self) -> bool:
        group = self._host._group()
        return bool(self._can_disc() and group and 0 < len(self._host._selected) < len(group.group.files))

    def _can_merge(self) -> bool:
        return not self._host.busy and len(self._host.selected_group_ids) > 1

    # These projections only enable controls. The command methods below repeat
    # eligibility checks because a menu can outlive the selection which opened it.
    canSplit = Property(bool, _can_split, notify=changed)
    canMerge = Property(bool, _can_merge, notify=changed)
    canDisc = Property(bool, _can_disc, notify=changed)
    discVisible = Property(bool, lambda self: self._disc_snapshot is not None, notify=changed)
    discValue = Property(int, lambda self: self._disc_value, notify=changed)
    discError = Property(str, lambda self: self._disc_error, notify=changed)

    def _warning(self, ids: tuple[str, ...]) -> str:
        state = self._host.session_state
        groups = [group for group in state.groups if group.group.group_id in ids]
        choices = regroup_candidate_decisions(state, ids)
        mappings = sum(group.manual_track_mapping is not None for group in groups)

        if not choices and not mappings and not any(group.selected_release for group in groups):
            return ""

        return (
            "Regrouping clears the selected releases and track mappings. "
            f"{choices} candidate field choices will require review again; "
            f"{mappings} manual track mappings will be removed. "
            "Manual values, Clear, Keep existing and filename choices are retained."
        )

    @Slot()
    def split(self) -> None:
        host = self._host
        group = host._group()

        if not self._can_split() or group is None:
            return

        # Compute the immutable proposal before asking for confirmation. Cancel
        # then discards only this local value, leaving the session unchanged.
        try:
            updated = split_session_group(
                host.session_state, group.group.group_id, host._selected, host.app_settings.rename
            )
        except ValueError as error:
            host.set_status(str(error))
            return

        warning = self._warning((group.group.group_id,))

        if warning:
            host.request_confirmation("Split group and reset matching?", warning, lambda: host.set_state(updated))
        else:
            host.set_state(updated)

    @Slot()
    def merge(self) -> None:
        host = self._host

        if not self._can_merge():
            return

        ids = host.selected_group_ids

        try:
            updated = merge_session_groups(host.session_state, ids, host.app_settings.rename)
        except ValueError as error:
            host.set_status(str(error))
            return

        labels = [
            f"{group.group.album_title or 'Untitled album'} — {len(group.group.files)} files"
            for group in host.session_state.groups
            if group.group.group_id in ids
        ]
        host.request_confirmation(
            "Merge groups", "\n".join((*labels, "", self._warning(ids))), lambda: host.set_state(updated)
        )

    @Slot()
    def beginDisc(self) -> None:
        group = self._host._group()

        if not self._can_disc() or group is None:
            return

        # Retain the exact session which the dialogue describes; committing a
        # stale disc hint must not silently target a newly selected album.
        self._disc_snapshot = self._host.session_state
        self._disc_value = group.disc_number_override or 0
        self._disc_error = ""
        self.changed.emit()

    @Slot(int, result=bool)
    def commitDisc(self, number: int) -> bool:
        host = self._host
        group = host._group()

        if self._disc_snapshot is None or host.session_state is not self._disc_snapshot or group is None:
            self._disc_error = "The group changed. Cancel and open the disc hint again."
            self.changed.emit()
            return False

        try:
            if not 0 <= number <= 9999:
                raise ValueError("Choose a disc number between 1 and 9999, or Auto (0).")

            updated = set_disc_override(self._disc_snapshot, group.group.group_id, number or None)
        except ValueError as error:
            self._disc_error = str(error)
            self.changed.emit()
            return False

        self._disc_snapshot = None
        host.set_state(updated)
        return True

    @Slot()
    def cancelDisc(self) -> None:
        self._disc_snapshot = None
        self.changed.emit()
