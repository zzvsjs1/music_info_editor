"""Blank physical tag text remains editable and survives unrelated approved changes."""

import pytest
from mutagen.apev2 import APEv2
from mutagen.id3 import ID3, TIT2, TPE1
from mutagen.wave import WAVE

from metadata_polisher.application.changes import RenameDecision, build_change_set
from metadata_polisher.application.review import set_manual_decision
from metadata_polisher.domain.media import LocalMediaFile
from metadata_polisher.domain.metadata import FieldReadState, MetadataField
from metadata_polisher.execution.cancellation import NeverCancelledToken
from metadata_polisher.execution.events import FileApplyStatus
from metadata_polisher.formats.flac import FlacAdapter
from metadata_polisher.formats.mp3 import Mp3Adapter
from metadata_polisher.formats.mp4 import Mp4Adapter
from metadata_polisher.formats.registry import FormatRegistry
from metadata_polisher.formats.tak import TakAdapter
from metadata_polisher.formats.wave import WaveAdapter
from metadata_polisher.infrastructure.transaction import BackupPolicy, TransactionalFileWriter
from metadata_polisher.scanner.scanner import scan_media
from metadata_polisher.session.review_previews import local_reviews
from tests.integration.formats.test_format_contract import make_wave_fixture
from tests.integration.formats.test_transaction_with_adapter import RecordingEventSink
from tests.unit.formats.test_flac import FakeFlacFile
from tests.unit.formats.test_mp3 import FakeMp3File
from tests.unit.formats.test_mp4 import FakeMp4File
from tests.unit.formats.test_tak import FakeTakFile


def test_manual_edit_of_physically_blank_title_preserves_mixed_artist_values(tmp_path):
    path = tmp_path / "01. Opening.wav"
    make_wave_fixture(path)
    audio = WAVE(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[" \t "]))
    audio.tags.add(TPE1(encoding=3, text=["Composer", "  "]))
    audio.save()

    (source,) = scan_media(tmp_path, FormatRegistry()).supported_files
    changes = _check_reviews(source)
    result = TransactionalFileWriter().apply_file(
        source, changes, WaveAdapter(), BackupPolicy(False, None, tmp_path, "whitespace"),
        NeverCancelledToken(), RecordingEventSink(),
    )
    assert result.status is FileApplyStatus.SUCCEEDED
    saved = WAVE(path)
    assert list(saved.tags["TIT2"].text) == ["Opening"]
    assert list(saved.tags["TPE1"].text) == ["Composer", "  "]


@pytest.mark.parametrize("format_id", ("flac", "mp3", "mp4", "tak"))
def test_format_read_results_keep_blank_text_reviewable(tmp_path, format_id):
    path = tmp_path / f"example.{format_id}"
    if format_id == "flac":
        adapter = FlacAdapter(loader=lambda _: FakeFlacFile({"TITLE": [" \t "], "ARTIST": ["Composer", "  "]}))
    elif format_id == "mp3":
        frames = ID3()
        frames.add(TIT2(encoding=3, text=[" \t "]))
        frames.add(TPE1(encoding=3, text=["Composer", "  "]))
        adapter = Mp3Adapter(loader=lambda _: FakeMp3File(frames))
    elif format_id == "mp4":
        adapter = Mp4Adapter(loader=lambda _: FakeMp4File({"©nam": [" \t "], "©ART": ["Composer", "  "]}))
    else:
        tags = APEv2()
        tags["Title"] = " \t "
        tags["Artist"] = ["Composer", "  "]
        adapter = TakAdapter(loader=lambda _: FakeTakFile(tags))

    _check_reviews(LocalMediaFile(path=path, format_id=format_id, read_result=adapter.read(path)))


def _check_reviews(source):
    reviews = local_reviews(source).reviews
    title = next(review for review in reviews if review.field is MetadataField.TITLE)
    artists = next(review for review in reviews if review.field is MetadataField.ARTISTS)
    assert title.read_state is FieldReadState.PRESENT
    assert title.existing_value == " \t "
    assert artists.existing_value == ("Composer", "  ")

    edited = tuple(set_manual_decision(review, "Opening") if review is title else review for review in reviews)
    changes = build_change_set(source, edited, RenameDecision.KEEP_FILENAME)
    assert [(change.field, change.old_value, change.new_value) for change in changes.metadata_changes] == [
        (MetadataField.TITLE, " \t ", "Opening"),
    ]
    return changes
