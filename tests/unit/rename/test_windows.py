import pytest

from metadata_polisher.rename.windows import (
    FilenameIssueCode,
    sanitise_windows_filename,
    validate_windows_filename_collision,
)


def test_invalid_windows_characters_use_deterministic_full_width_replacements() -> None:
    result = sanitise_windows_filename('a<b>c:d"e/f\\g|h?i*j.flac')

    assert result.is_valid
    assert result.sanitised_name == "a＜b＞c：d＂e／f＼g｜h？i＊j.flac"
    assert result.reason_codes == (FilenameIssueCode.INVALID_CHARACTERS_REPLACED,)


def test_sanitising_a_stem_does_not_change_the_extension() -> None:
    result = sanitise_windows_filename("question?.FLAC")

    assert result.sanitised_name == "question？.FLAC"
    assert result.original_extension == ".FLAC"
    assert result.sanitised_extension == ".FLAC"


@pytest.mark.parametrize("name", ("CON", "con.mp3", "PRN.flac", "AUX.wav", "NUL.m4a", "COM1.tak", "LPT9.mp3"))
def test_reserved_device_name_is_repaired_even_when_it_has_an_extension(name: str) -> None:
    result = sanitise_windows_filename(name)

    assert result.is_valid
    assert result.sanitised_name == f"_{name}"
    assert FilenameIssueCode.RESERVED_DEVICE_NAME_REPAIRED in result.reason_codes


@pytest.mark.parametrize("name", ("COM10.mp3", "LPT0.flac", "CONCERT.wav"))
def test_similar_non_reserved_names_are_unchanged(name: str) -> None:
    result = sanitise_windows_filename(name)

    assert result.is_valid
    assert result.sanitised_name == name
    assert result.reason_codes == ()


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("title.flac.", "title.flac"),
        ("title.flac ", "title.flac"),
        ("title.flac.  ", "title.flac"),
    ),
)
def test_trailing_dots_and_spaces_are_repaired_explicitly(name: str, expected: str) -> None:
    result = sanitise_windows_filename(name)

    assert result.is_valid
    assert result.sanitised_name == expected
    assert FilenameIssueCode.TRAILING_DOT_OR_SPACE_REPAIRED in result.reason_codes


def test_control_characters_are_rejected_instead_of_silently_removed() -> None:
    result = sanitise_windows_filename("bad\x00name.flac")

    assert not result.is_valid
    assert result.sanitised_name is None
    assert result.reason_codes == (FilenameIssueCode.CONTROL_CHARACTER,)


def test_unpaired_surrogate_is_returned_as_a_structured_blocker() -> None:
    result = sanitise_windows_filename("bad\ud800name.flac")

    assert not result.is_valid
    assert result.sanitised_name is None
    assert result.reason_codes == (FilenameIssueCode.INVALID_UNICODE,)


@pytest.mark.parametrize("name", ("", ".", "..", " . "))
def test_empty_component_after_repairs_is_rejected(name: str) -> None:
    result = sanitise_windows_filename(name)

    assert not result.is_valid
    assert result.sanitised_name is None
    assert FilenameIssueCode.EMPTY_COMPONENT in result.reason_codes


# 250 ASCII stem characters plus the five-character .flac extension
# exactly meet the 255 UTF-16-unit component boundary.
def test_component_at_255_utf16_code_units_is_accepted() -> None:
    name = f"{'a' * 250}.flac"

    result = sanitise_windows_filename(name)

    assert result.is_valid
    assert result.sanitised_name == name


def test_overlong_component_is_rejected_without_truncating_title_or_extension() -> None:
    name = f"{'a' * 251}.flac"

    result = sanitise_windows_filename(name)

    assert not result.is_valid
    assert result.sanitised_name is None
    assert result.original_extension == ".flac"
    assert result.reason_codes == (FilenameIssueCode.COMPONENT_TOO_LONG,)


# An emoji takes two UTF-16 code units: 254 + 2 = 256, although Python
# len would count only 255 characters. Windows policy must reject it.
def test_astral_characters_count_as_two_utf16_code_units() -> None:
    result = sanitise_windows_filename(f"{'a' * 254}😀")

    assert not result.is_valid
    assert result.reason_codes == (FilenameIssueCode.COMPONENT_TOO_LONG,)


def test_collision_validation_uses_case_insensitive_windows_names() -> None:
    result = validate_windows_filename_collision(
        "Song.flac",
        ("cover.jpg", "song.FLAC"),
    )

    assert not result.is_valid
    assert result.destination_name == "Song.flac"
    assert result.colliding_names == ("song.FLAC",)
    assert result.reason_codes == (FilenameIssueCode.DESTINATION_COLLISION,)
    assert result.suggested_name is None


def test_collision_validation_does_not_treat_current_source_as_a_collision() -> None:
    result = validate_windows_filename_collision(
        "Song.flac",
        ("Song.flac", "cover.jpg"),
        current_name="song.FLAC",
    )

    assert result.is_valid
    assert result.colliding_names == ()
    assert result.reason_codes == ()


# Only one existing entry can be the source itself. A second Windows-
# equivalent name must remain visible as a blocking destination conflict.
def test_collision_validation_exempts_exactly_one_current_source() -> None:
    result = validate_windows_filename_collision(
        "SONG.flac",
        ("Song.flac", "song.FLAC"),
        current_name="Song.flac",
    )

    assert not result.is_valid
    assert result.colliding_names == ("song.FLAC",)
    assert result.reason_codes == (FilenameIssueCode.DESTINATION_COLLISION,)


# Filename equivalence must not reuse matching normalisation: linguistic
# sharp-s expansion would falsely identify these two names as one path.
def test_collision_key_does_not_apply_linguistic_sharp_s_expansion() -> None:
    result = validate_windows_filename_collision(
        "STRASSE.flac",
        ("Straße.flac",),
    )

    assert result.is_valid


def test_collision_validation_is_pure_and_does_not_invent_a_suffix() -> None:
    existing_names = ("Track.flac",)

    first = validate_windows_filename_collision("track.FLAC", existing_names)
    second = validate_windows_filename_collision("track.FLAC", existing_names)

    assert first == second
    assert first.destination_name == "track.FLAC"
    assert first.suggested_name is None
