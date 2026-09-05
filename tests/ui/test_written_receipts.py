"""Written means a verified disk receipt; later pending edits remain visible."""

from dataclasses import replace
from pathlib import Path

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import set_manual_decision
from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.session.state import GroupSelection, ReviewedFileState, SessionState, VerifiedWriteReceipt
from metadata_polisher.ui.main_window import MainWindow
from tests.ui.test_models import make_group_state, make_reviews, make_source


# Written acknowledges one verified source snapshot. A new pending edit must
# take precedence so a historical receipt never appears to confirm unsaved values.
def test_written_status_uses_exact_receipt_and_yields_to_pending_edits(qtbot):
    source = make_source()
    group = make_group_state("album", source)
    state = SessionState(
        root=Path("library"), groups=(group,), selection=GroupSelection("album"),
        written_files=(VerifiedWriteReceipt(source, frozenset({MetadataField.TITLE}), False),),
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_session_state(state)
    window.file_table_view.selectRow(0)

    assert "Written" in window.file_model.index(0, 1).data()
    assert "Written" in window.diff_model.index(0, 1).data()
    assert "Written" not in window.diff_model.index(1, 1).data()

    reviews = make_reviews(source)
    reviews = (set_manual_decision(reviews[0], "Next pending title"), *reviews[1:])
    reviewed = ReviewedFileState(
        source.file_id, reviews=reviews,
        change_set=build_change_set(source, reviews, RenameDecision.KEEP_FILENAME),
    )
    window.set_session_state(replace(state, groups=(replace(group, reviewed_files=(reviewed,)),)))

    assert "Ready" in window.file_model.index(0, 1).data()
    assert "Replace" in window.diff_model.index(0, 1).data()
    assert window.diff_model.index(0, 4).data() == "Next pending title"


def test_rename_only_receipt_does_not_claim_metadata_fields_were_written(qtbot):
    source = make_source()
    group = make_group_state("album", source)
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_session_state(SessionState(
        root=Path("library"), groups=(group,), selection=GroupSelection("album"),
        written_files=(VerifiedWriteReceipt(source, frozenset(), True),),
    ))
    window.file_table_view.selectRow(0)

    assert "Written" in window.file_model.index(0, 1).data()
    assert all("Written" not in window.diff_model.index(row, 1).data()
               for row in range(window.diff_model.rowCount()))
