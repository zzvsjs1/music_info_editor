import pytest
from mutagen.id3 import (
    COMM,
    ID3,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
)

from metadata_polisher.domain.errors import MediaErrorCode
from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)
from metadata_polisher.formats.id3_codec import ID3_WRITE_VERSION, Id3TagCodec, Id3TagReadResult


def make_complete_id3() -> ID3:
    # Use real frame classes so list values, timestamp conversion and physical
    # identifiers are exercised independently of the container adapters.
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["決戦"]))
    tags.add(TPE1(encoding=3, text=["Artist One", "Artist Two"]))
    tags.add(TALB(encoding=3, text=["Soundtrack"]))
    tags.add(TPE2(encoding=3, text=["Album Artist"]))
    tags.add(TCOM(encoding=3, text=["Composer One", "Composer Two"]))
    tags.add(TRCK(encoding=3, text=["3/12"]))
    tags.add(TPOS(encoding=3, text=["2/4"]))
    tags.add(TDRC(encoding=3, text=["2024-01-30"]))
    tags.add(TCON(encoding=3, text=["Game", "Soundtrack"]))

    return tags


def test_id3_codec_reads_all_managed_v24_frames() -> None:
    result = Id3TagCodec().read(make_complete_id3())

    assert result.metadata == MetadataSnapshot(
        title="決戦",
        artists=("Artist One", "Artist Two"),
        album="Soundtrack",
        album_artists=("Album Artist",),
        composers=("Composer One", "Composer Two"),
        track=Position(number=3, total=12),
        disc=Position(number=2, total=4),
        date="2024-01-30",
        genres=("Game", "Soundtrack"),
    )
    assert all(state is FieldReadState.PRESENT for state in result.field_states.values())
    assert result.issues == ()
    assert ID3_WRITE_VERSION == 4


def test_id3_codec_keeps_missing_and_malformed_frames_distinct() -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[]))
    tags.add(TRCK(encoding=3, text=["not-a-position"]))

    result = Id3TagCodec().read(tags)

    assert result.field_states[MetadataField.TITLE] is FieldReadState.MISSING
    assert result.field_states[MetadataField.TRACK] is FieldReadState.UNREADABLE
    assert result.field_states[MetadataField.COMPOSERS] is FieldReadState.MISSING
    assert result.metadata.track == Position()
    assert {issue.code for issue in result.issues} == {MediaErrorCode.TAG_READ_FAILED}
    assert any("track" in issue.message.casefold() for issue in result.issues)


def test_id3_codec_does_not_stringify_malformed_text_frame_values() -> None:
    class MalformedTextFrame:
        text = [object()]

    result = Id3TagCodec().read({"TIT2": MalformedTextFrame()})  # type: ignore[arg-type]

    assert result.metadata.title is None
    assert result.field_states[MetadataField.TITLE] is FieldReadState.UNREADABLE
    assert any(issue.code is MediaErrorCode.TAG_READ_FAILED for issue in result.issues)


def test_id3_codec_converts_genre_property_failure_to_unreadable_issue() -> None:
    class MalformedGenreFrame:
        @property
        def genres(self) -> list[str]:
            raise ValueError("invalid numeric genre")

    result = Id3TagCodec().read({"TCON": MalformedGenreFrame()})  # type: ignore[arg-type]

    assert result.metadata.genres == ()
    assert result.field_states[MetadataField.GENRES] is FieldReadState.UNREADABLE
    assert any(issue.code is MediaErrorCode.TAG_READ_FAILED for issue in result.issues)


def test_id3_read_result_rejects_incomplete_field_states() -> None:
    with pytest.raises(ValueError, match="every MetadataField"):
        Id3TagReadResult(
            metadata=MetadataSnapshot(),
            field_states={MetadataField.TITLE: FieldReadState.MISSING},
        )


def test_id3_verify_rejects_unreadable_position_that_looks_like_requested_clear() -> None:
    tags = ID3()
    tags.add(TRCK(encoding=3, text=["not-a-position"]))

    result = Id3TagCodec().verify(
        tags,
        expected=MetadataSnapshot(track=Position()),
        changed_fields=frozenset({MetadataField.TRACK}),
    )

    assert not result.ok
    assert {issue.code for issue in result.issues} == {MediaErrorCode.VERIFICATION_FAILED}


def test_id3_codec_writes_only_changed_frames_and_preserves_unmanaged_frames() -> None:
    tags = make_complete_id3()
    tags.add(COMM(encoding=3, lang="eng", desc="review", text=["preserve me"]))
    original_title = tags.get("TIT2")
    original_comment = tags.getall("COMM")
    changes = (
        MetadataChange(
            field=MetadataField.ARTISTS,
            old_value=("Artist One", "Artist Two"),
            new_value=("Revised Artist", "Guest Artist"),
        ),
        MetadataChange(
            field=MetadataField.DISC,
            old_value=Position(number=2, total=4),
            new_value=Position(number=1, total=3),
        ),
    )

    Id3TagCodec().write_changes(tags, changes)

    artist_frame = tags.get("TPE1")
    disc_frame = tags.get("TPOS")
    assert artist_frame is not None
    assert disc_frame is not None
    assert tuple(str(value) for value in artist_frame.text) == ("Revised Artist", "Guest Artist")
    assert tuple(str(value) for value in disc_frame.text) == ("1/3",)
    assert tags.get("TIT2") is original_title
    assert tags.getall("COMM") == original_comment


@pytest.mark.parametrize(
    ("field", "new_value", "frame_id", "expected_text"),
    [
        (MetadataField.TITLE, "Title", "TIT2", ("Title",)),
        (MetadataField.ARTISTS, ("Artist One", "Artist Two"), "TPE1", ("Artist One", "Artist Two")),
        (MetadataField.ALBUM, "Album", "TALB", ("Album",)),
        (MetadataField.ALBUM_ARTISTS, ("Album Artist",), "TPE2", ("Album Artist",)),
        (MetadataField.COMPOSERS, ("Composer",), "TCOM", ("Composer",)),
        (MetadataField.TRACK, Position(number=3, total=12), "TRCK", ("3/12",)),
        (MetadataField.DISC, Position(number=2, total=4), "TPOS", ("2/4",)),
        (MetadataField.DATE, "2024-01-30", "TDRC", ("2024-01-30",)),
        (MetadataField.GENRES, ("Game", "Soundtrack"), "TCON", ("Game", "Soundtrack")),
    ],
)
def test_id3_codec_writes_each_managed_field_to_its_canonical_v24_frame(
    field: MetadataField,
    new_value: object,
    frame_id: str,
    expected_text: tuple[str, ...],
) -> None:
    tags = ID3()

    Id3TagCodec().write_changes(
        tags,
        (MetadataChange(field=field, old_value=None, new_value=new_value),),
    )

    frame = tags.get(frame_id)
    assert frame is not None
    assert frame.encoding == 3
    assert tuple(str(value) for value in frame.text) == expected_text


@pytest.mark.parametrize(
    ("field", "new_value", "frame_id", "expected_text"),
    [
        (MetadataField.TRACK, Position(number=7), "TRCK", "7"),
        (MetadataField.DISC, Position(number=2), "TPOS", "2"),
    ],
)
def test_id3_codec_preserves_partial_position_semantics_in_raw_frame(
    field: MetadataField,
    new_value: Position,
    frame_id: str,
    expected_text: str,
) -> None:
    tags = ID3()

    Id3TagCodec().write_changes(
        tags,
        (MetadataChange(field=field, old_value=Position(), new_value=new_value),),
    )

    frame = tags.get(frame_id)
    assert frame is not None
    assert tuple(str(value) for value in frame.text) == (expected_text,)


@pytest.mark.parametrize("field", [MetadataField.TRACK, MetadataField.DISC])
def test_id3_total_without_number_is_not_written_as_an_undefined_empty_prefix(field) -> None:
    tags = ID3()

    with pytest.raises(ValueError, match="total without a number"):
        Id3TagCodec().write_changes(tags, (MetadataChange(field, Position(), Position(total=12)),))

    assert not tags


def test_id3_codec_clear_removes_only_the_changed_managed_frame() -> None:
    tags = make_complete_id3()
    original_album = tags.get("TALB")

    Id3TagCodec().write_changes(
        tags,
        (
            MetadataChange(
                field=MetadataField.COMPOSERS,
                old_value=("Composer One", "Composer Two"),
                new_value=(),
            ),
        ),
    )

    assert tags.get("TCOM") is None
    assert tags.get("TALB") is original_album


@pytest.mark.parametrize(
    ("field", "frame_id", "frame"),
    [
        (MetadataField.TRACK, "TRCK", TRCK(encoding=3, text=["2/10"])),
        (MetadataField.DISC, "TPOS", TPOS(encoding=3, text=["1/2"])),
    ],
)
def test_id3_codec_validates_position_before_removing_existing_frame(
    field: MetadataField,
    frame_id: str,
    frame,
) -> None:
    tags = ID3()
    tags.add(frame)
    original = tags.get(frame_id)

    with pytest.raises(TypeError):
        Id3TagCodec().write_changes(
            tags,
            (MetadataChange(field=field, old_value=Position(), new_value="invalid"),),
        )

    assert tags.get(frame_id) is original


# A bad later change must leave an earlier valid frame untouched; validation
# belongs before the first mutation, not merely before the final save.
def test_id3_codec_validates_entire_batch_before_mutating_any_frame() -> None:
    tags = make_complete_id3()
    original_title = tags.get("TIT2")
    original_track = tags.get("TRCK")

    with pytest.raises(TypeError):
        Id3TagCodec().write_changes(
            tags,
            (
                MetadataChange(
                    field=MetadataField.TITLE,
                    old_value="決戦",
                    new_value="Revised",
                ),
                MetadataChange(
                    field=MetadataField.TRACK,
                    old_value=Position(number=3, total=12),
                    new_value="invalid",
                ),
            ),
        )

    assert tags.get("TIT2") is original_title
    assert tags.get("TRCK") is original_track
