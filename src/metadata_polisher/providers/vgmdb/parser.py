"""Pure parsing of public VGMdb HTML into provider-neutral models."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from metadata_polisher.domain.matching import (
    ComposerCredit,
    CreditScope,
    LocalisedText,
    ProviderTrack,
    ReleaseCandidate,
    ReleaseMedium,
)

ENGINE_ID = "vgmdb"
SOURCE_ID = "vgmdb"

_BASE_URL = "https://vgmdb.net"
_ALBUM_PATH = re.compile(r"/album/(\d+)/?")
_DISC_LABEL = re.compile(r"Disc\s+(\d+)", re.IGNORECASE)
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})


@dataclass
class _Node:
    tag: str
    attributes: dict[str, str]
    children: list[_Node | str] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Build the small DOM subset needed by the fixture-isolated parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(tag="document", attributes={})
        self._stack = [self.root]

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        node = _Node(
            tag=tag.casefold(),
            attributes={name.casefold(): value or "" for name, value in attrs},
        )
        self._stack[-1].children.append(node)

        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        node = _Node(
            tag=tag.casefold(),
            attributes={name.casefold(): value or "" for name, value in attrs},
        )
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()

        # Close the matching open ancestor and anything nested beneath it. This
        # tolerates omitted HTML end tags without detaching later sibling rows.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == wanted:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


@dataclass(frozen=True)
class _TrackVariant:
    number: int | None
    title: str | None
    duration_seconds: float | None
    printed_number: str | None


@dataclass(frozen=True)
class _MediumVariant:
    number: int | None
    title: str | None
    tracks: tuple[_TrackVariant, ...]


@dataclass
class _MergedTrack:
    number: int | None
    titles: list[LocalisedText]
    duration_seconds: float | None
    printed_number: str | None


@dataclass
class _MergedMedium:
    number: int | None
    title: str | None
    tracks: dict[tuple[str, int], _MergedTrack]


def _document(html: str) -> _Node:
    if not isinstance(html, str):
        raise TypeError("VGMdb HTML must be a string")

    parser = _DocumentParser()
    parser.feed(html)
    parser.close()

    return parser.root


def _nodes(root: _Node) -> Iterator[_Node]:
    for child in root.children:
        if isinstance(child, str):
            continue

        yield child
        yield from _nodes(child)


def _has_class(node: _Node, class_name: str) -> bool:
    return class_name in node.attributes.get("class", "").split()


def _find_all(
    root: _Node,
    *,
    tag: str | None = None,
    element_id: str | None = None,
    class_name: str | None = None,
) -> list[_Node]:
    return [
        node
        for node in _nodes(root)
        if (tag is None or node.tag == tag)
        and (element_id is None or node.attributes.get("id") == element_id)
        and (class_name is None or _has_class(node, class_name))
    ]


def _direct_nodes(root: _Node, *, tag: str | None = None) -> list[_Node]:
    return [
        child
        for child in root.children
        if isinstance(child, _Node) and (tag is None or child.tag == tag)
    ]


def _text(root: _Node) -> str:
    parts: list[str] = []

    def append_text(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                append_text(child)

    append_text(root)

    # Join inline fragments first, then collapse whitespace. Adding spaces
    # between every element would split names styled across nested spans.
    return " ".join("".join(parts).split())


def _language(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None

    normalised = value.strip().replace("_", "-").casefold()

    if normalised in {"english", "en"}:
        return "en", "Latn"

    if normalised in {"japanese", "ja"}:
        return "ja", "Jpan"

    if normalised in {"romaji", "romanized", "romanised", "ja-latn"}:
        return "ja", "Latn"

    return normalised or None, None


def _titles(root: _Node) -> tuple[LocalisedText, ...]:
    titles: list[LocalisedText] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for element in _find_all(root, class_name="albumtitle"):
        value = _text(element)

        if not value:
            continue

        language, script = _language(element.attributes.get("lang"))
        key = value, language, script

        if key in seen:
            continue

        seen.add(key)
        titles.append(LocalisedText(value=value, language=language, script=script))

    return tuple(titles)


def _album_id_from_url(url: str) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname.casefold() if parsed.hostname is not None else None

    if parsed.scheme != "https" or hostname not in {"vgmdb.net", "www.vgmdb.net"}:
        raise ValueError("VGMdb album URL must use the public HTTPS host")

    match = _ALBUM_PATH.fullmatch(parsed.path)

    if match is None or parsed.query or parsed.fragment:
        raise ValueError("VGMdb album URL must identify one numeric album")

    return match.group(1)


def _album_link(root: _Node) -> tuple[str, str] | None:
    for link in _find_all(root, tag="a"):
        href = link.attributes.get("href")

        if not href:
            continue

        absolute_url = urljoin(f"{_BASE_URL}/", href)

        try:
            release_id = _album_id_from_url(absolute_url)
        except ValueError:
            continue

        return release_id, f"{_BASE_URL}/album/{release_id}"

    return None


def _normalise_date(value: str) -> str | None:
    candidate = " ".join(value.split())

    if not candidate or candidate.casefold() in {"n/a", "tba", "unknown", "unreleased"}:
        return None

    if re.fullmatch(r"\d{4}", candidate):
        return candidate

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return candidate

    for date_format in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(candidate, date_format).date().isoformat()
        except ValueError:
            continue

    return candidate


def _date_from_node(root: _Node) -> str | None:
    for node in _nodes(root):
        title = node.attributes.get("title")

        if title and re.fullmatch(r"\d{4}(?:-\d{2}-\d{2})?", title.strip()):
            return _normalise_date(title)

    return _normalise_date(_text(root))


def _row_cells(row: _Node) -> list[_Node]:
    return _direct_nodes(row, tag="td")


def _search_date(row: _Node) -> str | None:
    labelled_cells = [
        cell
        for cell in _find_all(row, tag="td")
        if _has_class(cell, "release-date") or _has_class(cell, "released")
    ]

    if labelled_cells:
        return _date_from_node(labelled_cells[0])

    cells = _row_cells(row)

    if len(cells) >= 2:
        return _date_from_node(cells[-2])

    return None


def parse_search_results(html: str) -> tuple[ReleaseCandidate, ...]:
    """Parse lightweight album rows while retaining each printing identity."""
    document = _document(html)
    candidate_rows: list[_Node] = []

    for row in _find_all(document, tag="tr"):
        relation = row.attributes.get("rel", "")

        if relation.startswith("rel_") or _album_link(row) is not None:
            candidate_rows.append(row)

    page_text = _text(document).casefold()

    if not candidate_rows:
        zero_results = re.search(r"\b(?:0|no)\s+album\s+results?\b", page_text)

        if zero_results is not None:
            return ()

        raise ValueError("VGMdb search response does not contain a valid album-results shape")

    candidates: list[ReleaseCandidate] = []

    for row in candidate_rows:
        identity = _album_link(row)
        titles = _titles(row)

        if identity is None or not titles:
            raise ValueError("VGMdb album result is missing its identity or title")

        release_id, source_url = identity
        candidates.append(
            ReleaseCandidate(
                engine_id=ENGINE_ID,
                source_id=SOURCE_ID,
                release_id=release_id,
                titles=titles,
                album_artists=(),
                date=_search_date(row),
                media=(),
                source_url=source_url,
            )
        )

    return tuple(candidates)


def _visible_label(cell: _Node) -> str:
    for element in _nodes(cell):
        language = element.attributes.get("lang", "").casefold()

        if language == "en":
            value = _text(element)

            if value:
                return value

    return _text(cell)


def _information_value(document: _Node, label: str) -> _Node | None:
    wanted = label.casefold()

    for table in _find_all(document, tag="table", element_id="album_infobit_large"):
        value = _labelled_value(table, wanted)

        if value is not None:
            return value

    return None


def _labelled_value(root: _Node, wanted: str) -> _Node | None:
    for row in _find_all(root, tag="tr"):
        cells = _row_cells(row)

        if len(cells) >= 2 and _visible_label(cells[0]).casefold() == wanted:
            return cells[1]

    return None


def _credit_value(document: _Node, role: str) -> _Node | None:
    wanted = role.casefold()

    for credits in _find_all(document, element_id="collapse_credits"):
        value = _labelled_value(credits, wanted)

        if value is not None:
            return value

    # Some album-level artist values are presented in the information table
    # rather than the expandable credits table, so retain that explicit fallback.
    return _information_value(document, role)


def _preferred_artist_name(link: _Node) -> str | None:
    names = _find_all(link, class_name="artistname")

    for wanted_language in ("en", "ja-latn", "ja", ""):
        for name in names:
            if name.attributes.get("lang", "").casefold() != wanted_language:
                continue

            value = _text(name)

            if value:
                return value

    return None


def _credit_names(document: _Node, role: str) -> tuple[str, ...]:
    value_cell = _credit_value(document, role)

    if value_cell is None:
        return ()

    names: list[str] = []

    for link in _find_all(value_cell, tag="a"):
        name = _preferred_artist_name(link)

        if name is not None and name not in names:
            names.append(name)

    return tuple(names)


def _positive_integer(value: str) -> int | None:
    candidate = value.strip()

    if not candidate.isascii() or not candidate.isdecimal():
        return None

    number = int(candidate)

    return number if number > 0 else None


def _duration_seconds(value: str) -> float | None:
    candidate = value.strip()

    if not candidate:
        return None

    parts = candidate.split(":")

    if len(parts) not in {2, 3} or any(not part.isascii() or not part.isdecimal() for part in parts):
        return None

    # Colon-separated times use base 60: 2:03 is 2 * 60 + 3 = 123 seconds.
    # Only minutes in MM:SS may exceed 59; subordinate components cannot.
    numbers = [int(part) for part in parts]

    if len(numbers) == 2:
        minutes, seconds = numbers

        if seconds >= 60:
            return None

        return float(minutes * 60 + seconds)

    hours, minutes, seconds = numbers

    if minutes >= 60 or seconds >= 60:
        return None

    return float(hours * 3600 + minutes * 60 + seconds)


def _table_tracks(table: _Node) -> tuple[_TrackVariant, ...]:
    tracks: list[_TrackVariant] = []

    for row in _find_all(table, tag="tr"):
        cells = _row_cells(row)

        if len(cells) < 2:
            continue

        printed_number = _text(cells[0]) or None
        number = _positive_integer(printed_number or "")

        if printed_number is not None and printed_number.strip() == "0":
            number = 0

        title = _text(cells[1]) or None
        time_elements = _find_all(row, class_name="time")
        duration_text = _text(time_elements[0]) if time_elements else _text(cells[-1])
        tracks.append(
            _TrackVariant(
                number=number,
                title=title,
                duration_seconds=_duration_seconds(duration_text),
                printed_number=printed_number,
            )
        )

    return tuple(tracks)


def _view_media(view: _Node) -> tuple[_MediumVariant, ...]:
    media: list[_MediumVariant] = []
    current_number: int | None = None
    current_title: str | None = None

    # VGMdb emits a disc marker, optional disc title, and track table in order.
    # Walking direct siblings keeps an absent title on one disc from shifting a
    # later title backwards onto the wrong medium.
    for child in _direct_nodes(view):
        if child.tag == "span":
            value = _text(child)
            disc_match = _DISC_LABEL.fullmatch(value)

            if disc_match is not None:
                current_number = int(disc_match.group(1))
                current_title = None
            elif _has_class(child, "label") and value:
                current_title = value

            continue

        if child.tag == "table" and _has_class(child, "role"):
            media.append(
                _MediumVariant(
                    number=current_number,
                    title=current_title,
                    tracks=_table_tracks(child),
                )
            )
            current_number = None
            current_title = None

    return tuple(media)


def _tracklist_languages(document: _Node) -> tuple[tuple[str | None, str | None], ...]:
    navigation = _find_all(document, element_id="tlnav")

    if not navigation:
        return ()

    return tuple(_language(_text(item)) for item in _find_all(navigation[0], tag="li"))


def _merged_media(document: _Node) -> tuple[ReleaseMedium, ...]:
    tracklists = _find_all(document, element_id="tracklist")

    if not tracklists:
        return ()

    tracklist = tracklists[0]
    views = [
        view
        for view in _direct_nodes(tracklist, tag="span")
        if any(_has_class(table, "role") for table in _find_all(view, tag="table"))
    ]

    if not views and any(_has_class(table, "role") for table in _find_all(tracklist, tag="table")):
        views = [tracklist]

    languages = _tracklist_languages(document)
    # Parallel language views describe the same media, not extra discs. Merge
    # them by source position where known, preserving first-seen display order.
    merged: dict[tuple[str, int], _MergedMedium] = {}

    for view_index, view in enumerate(views):
        if view_index < len(languages):
            language, script = languages[view_index]
        else:
            language, script = _language(view.attributes.get("lang"))

        for medium_index, medium in enumerate(_view_media(view), start=1):
            medium_key = ("number", medium.number) if medium.number is not None else ("row", medium_index)
            merged_medium = merged.get(medium_key)

            if merged_medium is None:
                merged_medium = _MergedMedium(
                    number=medium.number,
                    title=medium.title,
                    tracks={},
                )
                merged[medium_key] = merged_medium

            for track_index, track in enumerate(medium.tracks, start=1):
                # A row index aligns unnumbered language variants, but is not a
                # source track number. Separate key spaces prevent an unknown
                # first row from swallowing an explicitly numbered track one.
                track_key = ("number", track.number) if track.number is not None else ("row", track_index)
                merged_track = merged_medium.tracks.get(track_key)

                if merged_track is None:
                    merged_track = _MergedTrack(
                        number=track.number,
                        titles=[],
                        duration_seconds=track.duration_seconds,
                        printed_number=track.printed_number,
                    )
                    merged_medium.tracks[track_key] = merged_track

                if merged_track.duration_seconds is None and track.duration_seconds is not None:
                    merged_track.duration_seconds = track.duration_seconds

                if track.title is not None:
                    title = LocalisedText(value=track.title, language=language, script=script)

                    if title not in merged_track.titles:
                        merged_track.titles.append(title)

    return tuple(
        ReleaseMedium(
            medium_number=medium.number,
            title=medium.title,
            tracks=tuple(
                ProviderTrack(
                    track_number=track.number,
                    titles=tuple(track.titles),
                    artists=(),
                    # The album credits table supplies no per-track assignment.
                    # Even one album composer cannot establish exact track credit.
                    composers=(),
                    duration_seconds=track.duration_seconds,
                    printed_number=track.printed_number,
                )
                for track in medium.tracks.values()
            ),
            # The current HTML parser has no independently established total
            # count/completeness contract. Visible rows remain useful evidence.
            tracks_complete=False,
        )
        for medium in merged.values()
    )


def parse_album_detail(html: str, *, source_url: str) -> ReleaseCandidate:
    """Parse one selected album and merge parallel language tracklists."""
    release_id = _album_id_from_url(source_url)
    document = _document(html)
    headings = _find_all(document, tag="h1")
    titles = _titles(headings[0]) if headings else ()

    if not titles:
        raise ValueError("VGMdb album detail is missing its title variants")

    canonical_links = [
        link
        for link in _find_all(document, tag="link")
        if "canonical" in link.attributes.get("rel", "").split()
    ]

    # A redirect or changed page must still describe the selected album; do
    # not attach another record's titles or credits to its cached identity.
    if canonical_links:
        canonical_url = urljoin(f"{_BASE_URL}/", canonical_links[0].attributes.get("href", ""))

        if _album_id_from_url(canonical_url) != release_id:
            raise ValueError("VGMdb album detail identity does not match the selected URL")

    date_cell = _information_value(document, "Release Date")
    album_credits = tuple(
        ComposerCredit(names, CreditScope.ALBUM, role=role.casefold(),
                       record_id=release_id, source_url=f"{_BASE_URL}/album/{release_id}")
        for role in ("Composer", "Arranger", "Lyricist", "Performer")
        if (names := _credit_names(document, role))
    )

    return ReleaseCandidate(
        engine_id=ENGINE_ID,
        source_id=SOURCE_ID,
        release_id=release_id,
        titles=titles,
        album_artists=_credit_names(document, "Artist"),
        date=_date_from_node(date_cell) if date_cell is not None else None,
        media=_merged_media(document),
        source_url=f"{_BASE_URL}/album/{release_id}",
        album_credits=album_credits,
        media_complete=False,
    )


def is_access_challenge(html: str) -> bool:
    """Recognise access-interstitial markers without attempting to bypass them."""
    normalised = html.casefold()

    return any(
        marker in normalised
        for marker in (
            "cf-challenge",
            "cdn-cgi/challenge-platform",
            "<title>just a moment",
            "checking your browser before accessing vgmdb",
        )
    )
