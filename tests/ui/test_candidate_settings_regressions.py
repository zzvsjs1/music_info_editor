"""Candidate review and Settings keep feedback bounded and tied to current inputs."""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPlainTextEdit

from metadata_polisher.application.lookup import CandidateLookupResult
from metadata_polisher.domain.errors import Issue, MatchingErrorCode, ProviderErrorCode
from metadata_polisher.domain.matching import LocalisedText
from metadata_polisher.infrastructure.settings import AppSettings
from metadata_polisher.matching.release_scoring import ReleaseRanking
from metadata_polisher.providers.coordinator import (
    CandidateHydrationNotice,
    CandidateHydrationReasonCode,
    ProviderFailure,
    ProviderSearchSummary,
)
from metadata_polisher.ui.dialogs.candidate_dialog import CandidateDialog
from metadata_polisher.ui.dialogs.settings_dialog import SettingsDialog
from tests.unit.session.test_lookup_editing import make_selected_session


def candidate_results(*, notices=0):
    """Build real candidate identities with deliberately nonalphabetical scores."""
    base = make_selected_session().groups[0].candidate_lookup
    coordinated = base.lookup_result.candidates[0]
    entry = base.release_ranking.entries[0]
    candidates, entries, hydration = [], [], []

    for number, (score, title) in enumerate(((100.0, "Zulu"), (90.0, "Alpha"), (9.0, "Middle"))):
        release = replace(
            coordinated.candidate, release_id=f"release-{number}", titles=(LocalisedText(title, "en", "Latn"),),
        )
        provenance = tuple(replace(item, record_id=release.release_id) for item in coordinated.provenance)
        candidates.append(replace(coordinated, candidate=release, provenance=provenance))
        entries.append(replace(entry, release=release, result=replace(entry.result, score=score)))

    for number in range(notices):
        release = replace(coordinated.candidate, release_id=f"unloaded-release-{number}", media=())
        provenance = tuple(replace(item, record_id=release.release_id) for item in coordinated.provenance)
        candidates.append(replace(coordinated, candidate=release, provenance=provenance))
        hydration.append(CandidateHydrationNotice(
            (release.engine_id, release.source_id, release.release_id),
            CandidateHydrationReasonCode.PER_ENGINE_LIMIT_REACHED, None,
        ))

    return CandidateLookupResult(
        replace(base.lookup_result, candidates=tuple(candidates)),
        ReleaseRanking(tuple(entries), False), tuple(hydration), (),
    )


def test_many_candidate_notices_preserve_visible_actions_and_all_copyable_details(qtbot):
    dialog = CandidateDialog(candidate_results(notices=75))
    qtbot.addWidget(dialog)
    dialog.resize(960, 650)
    dialog.show()
    qtbot.wait(20)

    assert dialog.height() <= 650
    assert dialog.width() <= 960
    assert dialog.rect().contains(dialog.choose_button.mapTo(dialog, dialog.choose_button.rect().bottomRight()))
    panel = dialog.issues_label
    assert isinstance(panel, QPlainTextEdit)
    assert panel.isReadOnly()
    assert "unloaded-release-74" in panel.toPlainText()
    assert panel.verticalScrollBar().maximum() > 0

    panel.setFocus()
    qtbot.keyClick(panel, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    assert panel.textCursor().atEnd()
    assert panel.viewport().rect().contains(panel.cursorRect().center())


@pytest.mark.parametrize(
    ("order", "expected_scores", "selected_id"),
    (
        (Qt.SortOrder.AscendingOrder, [9.0, 90.0, 100.0], "release-2"),
        (Qt.SortOrder.DescendingOrder, [100.0, 90.0, 9.0], "release-0"),
    ),
)
def test_score_sort_is_numeric_and_keyboard_activation_retains_identity(qtbot, order, expected_scores, selected_id):
    dialog = CandidateDialog(candidate_results())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.table.sortByColumn(6, order)
    model = dialog.table.model()

    assert [model.index(row, 6).data(Qt.ItemDataRole.UserRole).result.score for row in range(3)] == expected_scores
    dialog.table.selectRow(0)

    with qtbot.waitSignal(dialog.candidate_selected) as selected:
        qtbot.keyClick(dialog.table, Qt.Key.Key_Return)

    assert selected.args[0] == ("vgmdb", "vgmdb", selected_id, 0)


def test_title_sort_remains_alphabetical_after_numeric_score_sort(qtbot):
    dialog = CandidateDialog(candidate_results())
    qtbot.addWidget(dialog)
    dialog.table.sortByColumn(6, Qt.SortOrder.DescendingOrder)
    dialog.table.sortByColumn(2, Qt.SortOrder.AscendingOrder)
    model = dialog.table.model()

    assert [model.index(row, 2).data() for row in range(3)] == ["Alpha", "Middle", "Zulu"]


@pytest.mark.parametrize(
    ("code", "advice"),
    (
        (ProviderErrorCode.NETWORK_TIMEOUT, "retry"),
        (ProviderErrorCode.ACCESS_DENIED, "settings"),
        (ProviderErrorCode.PROXY_CONNECTION_FAILED, "proxy"),
        (ProviderErrorCode.PROXY_AUTHENTICATION_REQUIRED, "credentials"),
        (ProviderErrorCode.TLS_VERIFICATION_FAILED, "certificate"),
    ),
)
def test_failed_search_offers_request_recovery_without_title_advice(qtbot, code, advice):
    base = candidate_results()
    result = CandidateLookupResult(
        replace(base.lookup_result, candidates=(), failures=(ProviderFailure("vgmdb", Issue(code, "Request failed.")),),
                summaries=(ProviderSearchSummary("vgmdb", 1, 0, 0),)),
        ReleaseRanking((), False), (),
        (Issue(MatchingErrorCode.NO_CANDIDATE, "No metadata provider returned a release candidate."),),
    )
    dialog = CandidateDialog(result)
    qtbot.addWidget(dialog)
    message = dialog.issues_label.text().casefold()

    assert advice in message
    assert "edit search terms" not in message


def test_successful_empty_search_offers_alternative_title_advice(qtbot):
    base = candidate_results()
    result = CandidateLookupResult(
        replace(base.lookup_result, candidates=(), failures=(),
                summaries=(ProviderSearchSummary("vgmdb", 1, 1, 0),)),
        ReleaseRanking((), False), (),
        (Issue(MatchingErrorCode.NO_CANDIDATE, "No metadata provider returned a release candidate."),),
    )
    dialog = CandidateDialog(result)
    qtbot.addWidget(dialog)

    assert "Edit search terms" in dialog.issues_label.text()
    assert "No matching releases" in dialog.issues_label.text()


@pytest.mark.parametrize("field", ("track_digits_spin", "disc_digits_spin"))
def test_pasted_digit_width_cannot_queue_impractical_filename_allocations(qtbot, field):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    control = getattr(dialog, field)
    control.setValue(2_147_483_647)

    assert 1 <= control.value() <= 10
    assert 1 <= dialog.settings().rename.minimum_track_digits <= 10
    assert 1 <= dialog.settings().rename.minimum_disc_digits <= 10


@pytest.mark.parametrize("field", ("proxy_username_edit", "proxy_password_edit"))
def test_credential_draft_edit_invalidates_previous_provider_success(qtbot, field):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.network_mode_combo.setCurrentIndex(dialog.network_mode_combo.findData("manual_proxy"))
    dialog.provider_test_status.setText("Synthetic provider test passed.")
    getattr(dialog, field).setText("synthetic-changed-credential")

    assert "not tested" in dialog.provider_test_status.text().casefold()
    assert "synthetic-changed-credential" not in dialog.provider_test_status.text()


def test_in_flight_provider_test_freezes_credential_draft_until_completion(qtbot):
    dialog = SettingsDialog(AppSettings())
    qtbot.addWidget(dialog)
    dialog.network_mode_combo.setCurrentIndex(dialog.network_mode_combo.findData("manual_proxy"))
    dialog.set_provider_test_running(True)

    assert not dialog.proxy_username_edit.isEnabled()
    assert not dialog.proxy_password_edit.isEnabled()
    assert not dialog.save_session_login_button.isEnabled()
    assert not dialog.forget_session_login_button.isEnabled()

    dialog.set_provider_test_running(False)
    dialog.provider_test_status.setText("Synthetic provider test passed.")
    dialog.proxy_password_edit.setText("synthetic-new-draft")

    assert dialog.proxy_password_edit.isEnabled()
    assert "not tested" in dialog.provider_test_status.text().casefold()
