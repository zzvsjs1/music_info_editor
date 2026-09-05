"""Deterministic Windows filename sanitisation and collision policy."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class FilenameIssueSeverity(Enum):
    """Whether a filename issue was repaired or must block a rename."""

    REPAIR = "repair"
    BLOCKING = "blocking"


class FilenameIssueCode(Enum):
    """Stable, human-debuggable reason codes for filename decisions."""

    INVALID_CHARACTERS_REPLACED = "invalid_characters_replaced"
    TRAILING_DOT_OR_SPACE_REPAIRED = "trailing_dot_or_space_repaired"
    RESERVED_DEVICE_NAME_REPAIRED = "reserved_device_name_repaired"
    CONTROL_CHARACTER = "control_character"
    INVALID_UNICODE = "invalid_unicode"
    EMPTY_COMPONENT = "empty_component"
    COMPONENT_TOO_LONG = "component_too_long"
    DESTINATION_COLLISION = "destination_collision"


@dataclass(frozen=True)
class FilenameIssue:
    """One deterministic repair or blocking filename finding."""

    code: FilenameIssueCode
    severity: FilenameIssueSeverity
    message: str


@dataclass(frozen=True)
class FilenameValidationResult:
    """Sanitised component and structured evidence explaining the outcome."""

    original_name: str
    sanitised_name: str | None
    issues: tuple[FilenameIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Return whether the component can safely proceed to collision checks."""
        return self.sanitised_name is not None and all(
            issue.severity is not FilenameIssueSeverity.BLOCKING for issue in self.issues
        )

    @property
    def reason_codes(self) -> tuple[FilenameIssueCode, ...]:
        """Expose stable reason codes without requiring callers to parse text."""
        return tuple(issue.code for issue in self.issues)

    @property
    def original_extension(self) -> str:
        """Return the source extension for explicit preservation checks."""
        return _filename_extension(self.original_name)

    @property
    def sanitised_extension(self) -> str | None:
        """Return the resulting extension, or ``None`` for a blocked name."""
        if self.sanitised_name is None:
            return None

        return _filename_extension(self.sanitised_name)


@dataclass(frozen=True)
class FilenameCollisionResult:
    """Pure collision decision; it deliberately contains no renamed suggestion."""

    destination_name: str
    colliding_names: tuple[str, ...] = ()
    suggested_name: None = None

    @property
    def is_valid(self) -> bool:
        """Return whether the destination is currently unoccupied."""
        return not self.colliding_names

    @property
    def reason_codes(self) -> tuple[FilenameIssueCode, ...]:
        """Return the blocking collision reason when applicable."""
        if self.colliding_names:
            return (FilenameIssueCode.DESTINATION_COLLISION,)

        return ()


# Full-width replacements retain readable punctuation while ensuring the
# rendered component cannot introduce directory separators or Windows syntax.
_INVALID_CHARACTER_TRANSLATION = str.maketrans(
    {
        "<": "＜",
        ">": "＞",
        ":": "：",
        '"': "＂",
        "/": "／",
        "\\": "＼",
        "|": "｜",
        "?": "？",
        "*": "＊",
    }
)

_INVALID_VISIBLE_CHARACTERS = frozenset('<>:"/\\|?*')
_RESERVED_EXACT_NAMES = frozenset({"con", "prn", "aux", "nul"})
_MAX_COMPONENT_UTF16_UNITS = 255


def _filename_extension(name: str) -> str:
    """Find an extension without treating invalid slashes as path separators."""
    final_dot = name.rfind(".")

    if final_dot <= 0 or final_dot == len(name) - 1:
        return ""

    return name[final_dot:]


def _is_reserved_device_name(name: str) -> bool:
    # Windows reserves device names even when they carry an extension. Stripping
    # spaces/dots from the base mirrors how Win32 resolves a component rather than
    # allowing a visually subtle device-path bypass.
    base_name = name.split(".", maxsplit=1)[0].rstrip(" .").casefold()

    if base_name in _RESERVED_EXACT_NAMES:
        return True

    if len(base_name) == 4 and base_name[:3] in {"com", "lpt"}:
        return base_name[3] in "123456789"

    return False


def _utf16_code_units(text: str) -> int:
    # NTFS's component limit is expressed in UTF-16 code units. Python's len()
    # counts an astral character once, so using it would accept some names that
    # Windows rejects as longer than 255 units.
    return len(text.encode("utf-16-le")) // 2


def sanitise_windows_filename(name: str) -> FilenameValidationResult:
    """Repair safe cases and return explicit blockers for unsafe components."""
    issues: list[FilenameIssue] = []
    candidate = name

    if any(character in _INVALID_VISIBLE_CHARACTERS for character in candidate):
        candidate = candidate.translate(_INVALID_CHARACTER_TRANSLATION)
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.INVALID_CHARACTERS_REPLACED,
                severity=FilenameIssueSeverity.REPAIR,
                message="Windows-invalid punctuation was replaced with full-width equivalents.",
            )
        )

    repaired_trailing = candidate.rstrip(" .")

    if repaired_trailing != candidate:
        candidate = repaired_trailing
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.TRAILING_DOT_OR_SPACE_REPAIRED,
                severity=FilenameIssueSeverity.REPAIR,
                message="Trailing dots or spaces were removed.",
            )
        )

    if candidate and _is_reserved_device_name(candidate):
        candidate = f"_{candidate}"
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.RESERVED_DEVICE_NAME_REPAIRED,
                severity=FilenameIssueSeverity.REPAIR,
                message="A reserved Windows device name was prefixed with an underscore.",
            )
        )

    # Safe visible repairs are recorded above. Invisible control characters
    # are blockers rather than silent deletions, so a surprising source value
    # remains something the user can inspect and correct.
    control_code_points = tuple(ord(character) for character in candidate if ord(character) < 32)

    if control_code_points:
        code_point_text = ", ".join(f"U+{code_point:04X}" for code_point in control_code_points)
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.CONTROL_CHARACTER,
                severity=FilenameIssueSeverity.BLOCKING,
                message=f"Control characters are not valid in Windows filenames: {code_point_text}.",
            )
        )

    if not candidate:
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.EMPTY_COMPONENT,
                severity=FilenameIssueSeverity.BLOCKING,
                message="The filename component is empty after deterministic repairs.",
            )
        )

    try:
        component_utf16_units = _utf16_code_units(candidate)
    except UnicodeEncodeError:
        component_utf16_units = None
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.INVALID_UNICODE,
                severity=FilenameIssueSeverity.BLOCKING,
                message="The filename contains an unpaired Unicode surrogate.",
            )
        )

    # Never shorten an overlong title automatically: truncation could remove
    # the extension or create a collision with another independently rendered
    # name. Return a blocker with the original suggestion intact.
    if component_utf16_units is not None and component_utf16_units > _MAX_COMPONENT_UTF16_UNITS:
        issues.append(
            FilenameIssue(
                code=FilenameIssueCode.COMPONENT_TOO_LONG,
                severity=FilenameIssueSeverity.BLOCKING,
                message="The filename component exceeds 255 UTF-16 code units.",
            )
        )

    has_blocker = any(issue.severity is FilenameIssueSeverity.BLOCKING for issue in issues)

    return FilenameValidationResult(
        original_name=name,
        sanitised_name=None if has_blocker else candidate,
        issues=tuple(issues),
    )


def windows_collision_key(name: str) -> str:
    """Normalise only the filename behaviours relevant to collision checks."""
    # Windows ordinal-insensitive comparison applies simple case mapping rather
    # than linguistic folding. In particular, ``Straße`` and ``STRASSE`` must not
    # collide merely because Python's casefold() expands sharp-s to two letters.
    return name.rstrip(" .").lower()


def validate_windows_filename_collision(
    destination_name: str,
    existing_names: Iterable[str],
    *,
    current_name: str | None = None,
) -> FilenameCollisionResult:
    """Check one destination without mutating input or inventing a suffix.

    ``current_name`` identifies the existing source component during an in-place
    rename. A case-only rename may therefore target its own source, while another
    occupied Windows-equivalent name remains a blocking collision.
    """
    destination_key = windows_collision_key(destination_name)
    current_key = windows_collision_key(current_name) if current_name is not None else None
    colliding_names = [
        existing_name
        for existing_name in existing_names
        if windows_collision_key(existing_name) == destination_key
    ]
    # Stable ordering makes repeated previews show the same collision evidence
    # even when directory enumeration order changes.
    colliding_names.sort(key=lambda existing_name: (existing_name.lower(), existing_name))

    if current_key is not None and destination_key == current_key and colliding_names:
        # Exempt one entry only: it represents the source that already occupies
        # its own name. Consuming every equivalent entry would hide a second real
        # destination conflict in a supplied directory snapshot.
        exact_source_index = next(
            (
                index
                for index, existing_name in enumerate(colliding_names)
                if existing_name == current_name
            ),
            0,
        )
        colliding_names.pop(exact_source_index)

    return FilenameCollisionResult(
        destination_name=destination_name,
        colliding_names=tuple(colliding_names),
    )
