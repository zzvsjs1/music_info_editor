from dataclasses import FrozenInstanceError

import pytest

from metadata_polisher.domain.metadata import (
    FieldReadState,
    MetadataChange,
    MetadataField,
    MetadataSnapshot,
    Position,
)


def test_metadata_field_values_are_stable() -> None:
    assert tuple(field.value for field in MetadataField) == (
        "title",
        "artists",
        "album",
        "album_artists",
        "composers",
        "track",
        "disc",
        "date",
        "genres",
    )


def test_negative_position_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="number cannot be negative"):
        Position(number=-1)

    with pytest.raises(ValueError, match="total cannot be negative"):
        Position(total=-1)


@pytest.mark.parametrize("invalid_value", [True, False, 1.5])
def test_position_rejects_bool_and_float_values(invalid_value: object) -> None:
    with pytest.raises(TypeError, match="Position number must be an integer or None"):
        Position(number=invalid_value)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Position total must be an integer or None"):
        Position(total=invalid_value)  # type: ignore[arg-type]


def test_metadata_snapshot_defaults_multi_value_fields_to_empty_tuples() -> None:
    snapshot = MetadataSnapshot()

    assert snapshot.artists == ()
    assert snapshot.album_artists == ()
    assert snapshot.composers == ()
    assert snapshot.genres == ()
    assert snapshot.track == Position()
    assert snapshot.disc == Position()


# Mutating the original list after construction probes defensive copying.
# A frozen outer dataclass alone would not protect the nested collection.
def test_metadata_snapshot_normalises_sequence_values_to_immutable_tuples() -> None:
    artists = ["Artist One", "Artist Two"]
    snapshot = MetadataSnapshot(artists=artists)  # type: ignore[arg-type]
    artists.append("Later Mutation")

    assert snapshot.artists == ("Artist One", "Artist Two")


# Strings are sequences in Python, but one artist name must not become
# a tuple of individual characters at the metadata boundary.
def test_metadata_snapshot_rejects_scalar_string_for_multi_value_field() -> None:
    with pytest.raises(TypeError, match="artists must be a sequence of strings, not a string"):
        MetadataSnapshot(artists="AB")  # type: ignore[arg-type]


@pytest.mark.parametrize("unordered_values", [{"B", "A"}, {"first": "A", "second": "B"}])
def test_metadata_snapshot_rejects_unordered_multi_value_collections(
    unordered_values: object,
) -> None:
    with pytest.raises(TypeError, match="ordered sequence"):
        MetadataSnapshot(artists=unordered_values)  # type: ignore[arg-type]


def test_metadata_snapshot_rejects_non_string_multi_value_items() -> None:
    with pytest.raises(TypeError, match="composers must contain only strings"):
        MetadataSnapshot(composers=("Composer", 42))  # type: ignore[arg-type]


def test_metadata_values_and_changes_are_immutable() -> None:
    snapshot = MetadataSnapshot(title="Original")
    change = MetadataChange(
        field=MetadataField.TITLE,
        old_value="Original",
        new_value="Revised",
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.title = "Mutated"

    with pytest.raises(FrozenInstanceError):
        change.new_value = "Mutated"


# These states lead to different review decisions: absence is not proof
# that an unreadable existing tag is safe to replace.
def test_missing_and_unreadable_states_are_distinct() -> None:
    assert FieldReadState.MISSING is not FieldReadState.UNREADABLE
    assert FieldReadState.MISSING.value == "missing"
    assert FieldReadState.UNREADABLE.value == "unreadable"
