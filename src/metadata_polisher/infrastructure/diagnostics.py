"""Allowlisted session diagnostics, secret redaction and memory-only operation IDs."""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Protocol, cast
from uuid import uuid4

from metadata_polisher.domain.metadata import metadata_value
from metadata_polisher.domain.review import FieldReviewState, FieldValue
from metadata_polisher.infrastructure.logging_setup import redact_sensitive_text
from metadata_polisher.matching.release_scoring import MatchEvidence
from metadata_polisher.matching.track_mapping import TrackMappingResult
from metadata_polisher.session.state import GroupState, ReleaseMediumIdentity


class OperationIdSource(Protocol):
    """Allocate an operation identity without coupling callers to its generation policy."""

    def next_id(self, kind: str) -> str:
        """Return a safe operation identity for the supplied operation kind."""
        ...


def _operation_kind(kind: str) -> str:
    if not isinstance(kind, str):
        raise TypeError("kind must be a string")

    normalised = kind.upper()

    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,23}", normalised) is None:
        raise ValueError("operation kind must contain only letters, digits and underscores")

    return normalised


class SequentialOperationIds:
    """Small deterministic fake whose counter spans all kinds within a test session."""

    def __init__(self) -> None:
        self._counter = 0

    def next_id(self, kind: str) -> str:
        operation_kind = _operation_kind(kind)
        self._counter += 1

        return f"{operation_kind}-{self._counter:04d}"


class ThreadSafeOperationIds:
    """Production identities with a fresh process-session prefix and locked counter."""

    def __init__(self) -> None:
        # Backups and reports can survive an application restart. A fresh UUID
        # avoids reusing their operation directories without writing a counter or
        # introducing persistent session state beside the portable executable.
        self._session_prefix = uuid4().hex
        self._counter = 0
        self._lock = Lock()

    def next_id(self, kind: str) -> str:
        operation_kind = _operation_kind(kind)

        with self._lock:
            self._counter += 1
            number = self._counter

        return f"{operation_kind}-{self._session_prefix}-{number:04d}"


def _sensitive_key(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.casefold())

    return (
        compact in {"auth", "apikey", "key", "sessionid"}
        or any(
            part in compact
            for part in (
                "authorization", "authorisation", "cookie", "header", "token",
                "password", "passwd", "secret", "credential",
            )
        )
        or compact.endswith("url")
    )


def sanitise_diagnostic_data(data: object) -> object:
    """Copy typed diagnostics into JSON-compatible values with recursive redaction.

    Unknown objects are never converted through repr/str because those methods
    can expose a provider response or credential-bearing transport object.
    Dataclass fields are walked individually so sensitive fields are replaced
    before their values enter the resulting data structure.
    """
    active: set[int] = set()

    def sanitise(value: object) -> object:
        if isinstance(value, Enum):
            return sanitise(value.value)

        if value is None or isinstance(value, (bool, int)):
            return value

        if isinstance(value, float):
            return value if math.isfinite(value) else "[NON-FINITE]"

        if isinstance(value, str):
            return redact_sensitive_text(value)

        if isinstance(value, Path):
            return redact_sensitive_text(str(value))

        identity = id(value)

        if identity in active:
            return "[CYCLE]"

        # Track only the current recursion path. Shared objects may appear in
        # several branches; only a reference back to an active ancestor is a cycle.
        active.add(identity)

        try:
            if isinstance(value, Mapping):
                copied: dict[str, object] = {}

                for raw_key, member in value.items():
                    key_value = raw_key.value if isinstance(raw_key, Enum) else raw_key

                    if not isinstance(key_value, (str, int, float, bool)):
                        continue

                    key = str(key_value)
                    copied[redact_sensitive_text(key)] = "[REDACTED]" if _sensitive_key(key) else sanitise(member)

                return copied

            if is_dataclass(value) and not isinstance(value, type):
                return {
                    item.name: "[REDACTED]" if _sensitive_key(item.name) else sanitise(getattr(value, item.name))
                    for item in fields(value)
                }

            if isinstance(value, (list, tuple)):
                return [sanitise(member) for member in value]

            # Avoid arbitrary repr/str methods: a transport or credential object
            # may include secret data even when its type is unknown here.
            return "[UNSUPPORTED]"
        finally:
            active.remove(identity)

    return sanitise(data)


def _release_identity(identity: ReleaseMediumIdentity) -> dict[str, object]:
    return {
        "engine_id": identity[0],
        "source_id": identity[1],
        "release_id": identity[2],
        "medium_index": identity[3],
    }


def _evidence_summary(evidence: Sequence[MatchEvidence]) -> list[dict[str, object]]:
    return [
        {"code": item.code, "contribution": item.contribution, "detail": item.detail}
        for item in evidence
    ]


def _mapping_summary(mapping: TrackMappingResult | None) -> dict[str, object] | None:
    if mapping is None:
        return None

    return {
        "selected_medium_index": mapping.selected_medium_index,
        "selected_medium_number": mapping.selected_medium_number,
        "classification": mapping.classification.value,
        "evidence": _evidence_summary(mapping.evidence),
        "unmatched_local_file_ids": list(mapping.unmatched_local_file_ids),
        "unmatched_provider_indexes": list(mapping.unmatched_provider_indexes),
        "mappings": [
            {
                "local_file_id": item.local_file_id,
                "provider_track_index": item.provider_track_index,
                "track_position": {"number": item.track_position.number, "total": item.track_position.total},
                "disc_position": {"number": item.disc_position.number, "total": item.disc_position.total},
                "score": item.score,
                "classification": item.classification.value,
                "evidence": _evidence_summary(item.evidence),
            }
            for item in mapping.mappings
        ],
    }


def _field_summary(review: FieldReviewState, final_value: FieldValue | None) -> dict[str, object]:
    return {
        "field": review.field.value,
        "read_state": review.read_state.value,
        "existing_value": review.existing_value,
        "final_value": final_value,
        "decision": review.decision.value,
        "decision_origin": review.decision_origin.value,
        "manual_value": review.manual_value,
        "requires_review": review.requires_review,
        "reason_codes": list(review.reason_codes),
        "selected_proposal_index": (
            review.proposals.index(review.selected_proposal)
            if review.selected_proposal is not None
            else None
        ),
        "proposals": [
            {
                "value": proposal.value,
                "confidence": proposal.confidence.value,
                "language": proposal.language,
                "script": proposal.script,
                "reason_codes": list(proposal.reason_codes),
                "provenance": [
                    {
                        "engine_id": origin.engine_id,
                        "source_id": origin.source_id,
                        "record_id": origin.record_id,
                        "operation_id": origin.operation_id,
                        "language": origin.language,
                    }
                    for origin in proposal.provenances
                ],
            }
            for proposal in review.proposals
        ],
    }


def build_group_diagnostic_summary(group: GroupState) -> dict[str, object]:
    """Project reproducible release, mapping and review evidence without writing files."""
    if not isinstance(group, GroupState):
        raise TypeError("group must be a GroupState")

    # This allowlist deliberately never serialises a provider candidate or a
    # session dataclass wholesale. Source URLs, transport headers and unrelated
    # provider payloads cannot accidentally appear when those models gain fields.
    summary: dict[str, object] = {
        "schema_version": 1,
        "group_id": group.group.group_id,
        "revision": group.revision,
        "requires_rescan": group.requires_rescan,
        "language_override": group.language_override,
        "disc_number_override": group.disc_number_override,
        "selected_release": _release_identity(group.selected_release.identity) if group.selected_release else None,
        "release_candidates": [
            {
                "identity": _release_identity(entry.identity),
                "score": entry.result.score,
                "classification": entry.result.classification.value,
                "evidence": _evidence_summary(entry.result.evidence),
            }
            for entry in group.release_ranking.entries
        ] if group.release_ranking is not None else [],
        "automatic_track_mapping": _mapping_summary(group.automatic_track_mapping),
        "effective_track_mapping": _mapping_summary(group.effective_track_mapping),
        "files": [
            {
                "file_id": file.file_id,
                "track_mapping_resolved": file.track_mapping_resolved,
                "fields": [
                    _field_summary(
                        review,
                        metadata_value(file.change_set.final_metadata, review.field)
                        if file.change_set is not None
                        else None,
                    )
                    for review in file.reviews
                ],
            }
            for file in group.reviewed_files
        ],
    }

    return cast(dict[str, object], sanitise_diagnostic_data(summary))
