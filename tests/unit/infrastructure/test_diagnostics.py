import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from metadata_polisher.domain.metadata import MetadataField
from metadata_polisher.domain.review import FieldDecisionKind
from metadata_polisher.infrastructure.diagnostics import (
    OperationIdSource,
    SequentialOperationIds,
    ThreadSafeOperationIds,
    build_group_diagnostic_summary,
    sanitise_diagnostic_data,
)
from metadata_polisher.infrastructure.settings import RenameSettings
from metadata_polisher.session.mapping_editing import apply_manual_track_mapping
from metadata_polisher.session.review_editing import apply_field_decision
from tests.unit.session.test_lookup_editing import make_selected_session
from tests.unit.session.test_mapping_editing import partial_session, resolved_mapping


def test_sequential_operation_ids_are_deterministic_across_operation_kinds() -> None:
    source: OperationIdSource = SequentialOperationIds()

    assert source.next_id("scan") == "SCAN-0001"
    assert source.next_id("LOOKUP") == "LOOKUP-0002"
    assert source.next_id("apply") == "APPLY-0003"
    assert SequentialOperationIds().next_id("scan") == "SCAN-0001"


def test_production_operation_ids_are_unique_across_threads_and_new_sessions() -> None:
    source: OperationIdSource = ThreadSafeOperationIds()

    # Concurrent allocation must preserve uniqueness and a gap-free counter.
    # A second source then checks the separate process-session identity prefix.
    with ThreadPoolExecutor(max_workers=8) as workers:
        identifiers = tuple(workers.map(lambda _index: source.next_id("apply"), range(128)))

    assert len(set(identifiers)) == 128
    assert all(re.fullmatch(r"APPLY-[0-9a-f]{32}-\d{4,}", identifier) for identifier in identifiers)
    assert {int(identifier.rsplit("-", 1)[1]) for identifier in identifiers} == set(range(1, 129))
    assert source.next_id("scan").endswith("-0129")
    other_session = ThreadSafeOperationIds().next_id("apply")
    assert other_session.rsplit("-", 1)[0] != identifiers[0].rsplit("-", 1)[0]


@pytest.mark.parametrize("kind", ["", "../apply", "scan/name", "apply\nlookup"])
def test_operation_ids_reject_unsafe_or_blank_operation_kinds(kind: str) -> None:
    for source in (SequentialOperationIds(), ThreadSafeOperationIds()):
        with pytest.raises(ValueError):
            source.next_id(kind)


def test_group_summary_preserves_release_scores_field_choices_and_provider_provenance() -> None:
    state = make_selected_session()
    original = state.groups[0]
    source = original.group.files[0]
    state = apply_field_decision(
        state, "album", source.file_id, MetadataField.TITLE, FieldDecisionKind.USE_PROPOSAL,
        RenameSettings(), proposal_index=1,
    )
    group = state.groups[0]
    summary = build_group_diagnostic_summary(group)

    assert summary["schema_version"] == 1
    assert summary["group_id"] == "album"
    assert summary["revision"] == group.revision
    ranked = summary["release_candidates"][0]
    assert ranked["identity"] == {
        "engine_id": "vgmdb", "source_id": "vgmdb", "release_id": "123", "medium_index": 0,
    }
    assert ranked["score"] == group.release_ranking.entries[0].result.score
    assert ranked["classification"] == group.release_ranking.entries[0].result.classification.value
    assert ranked["evidence"] == [
        {"code": item.code, "contribution": item.contribution, "detail": item.detail}
        for item in group.release_ranking.entries[0].result.evidence
    ]
    assert summary["selected_release"] == ranked["identity"]
    title = next(item for item in summary["files"][0]["fields"] if item["field"] == "title")
    reviewed_title = next(item for item in group.reviewed_files[0].reviews if item.field is MetadataField.TITLE)
    assert title["decision"] == "use_proposal"
    assert title["decision_origin"] == "user"
    assert title["reason_codes"] == list(reviewed_title.reason_codes)
    assert title["selected_proposal_index"] == 1
    proposal = title["proposals"][1]
    assert proposal["value"] == "Overture"
    assert proposal["confidence"] == reviewed_title.proposals[1].confidence.value
    assert proposal["provenance"][0] == {
        "engine_id": "vgmdb", "source_id": "vgmdb", "record_id": "123",
        "operation_id": "LOOKUP-0001", "language": "en",
    }

    encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False)
    assert "vgmdb.net" not in encoded
    assert "source_url" not in encoded
    assert "headers" not in encoded
    assert group.reviewed_files[0].proposals is original.reviewed_files[0].proposals
    assert group.selected_release is original.selected_release


def test_group_summary_shows_original_and_human_effective_mapping_without_writing_files(monkeypatch) -> None:
    state = partial_session()
    original = state.groups[0].automatic_track_mapping
    state = apply_manual_track_mapping(state, "album", resolved_mapping(state), RenameSettings())

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("Building a diagnostic summary must never emit a file")

    monkeypatch.setattr("builtins.open", unexpected_open)
    summary = build_group_diagnostic_summary(state.groups[0])
    automatic = summary["automatic_track_mapping"]
    effective = summary["effective_track_mapping"]

    assert automatic["mappings"] == []
    assert automatic["unmatched_local_file_ids"] == list(original.unmatched_local_file_ids)
    assert effective["unmatched_local_file_ids"] == []
    assert len(effective["mappings"]) == 1
    pair = effective["mappings"][0]
    assert pair["local_file_id"] == state.groups[0].group.files[0].file_id
    assert pair["provider_track_index"] == 0
    assert pair["track_position"] == {"number": 1, "total": 1}
    assert pair["classification"] == "high"
    assert pair["score"] == 0.0
    assert pair["evidence"][0]["code"] == "MANUAL_TRACK_ASSIGNMENT"
    assert pair["evidence"][0]["contribution"] == 0.0
    assert summary["files"][0]["track_mapping_resolved"] is True


# Nested structured fields and embedded error strings take different redaction
# paths; both must remove the synthetic secrets while retaining safe evidence.
def test_recursive_sanitisation_redacts_headers_tokens_credentials_and_embedded_text() -> None:
    data = {
        "provider": "MusicBrainz",
        "Authorization": "Bearer auth-secret",
        "Cookie": "session=cookie-secret",
        "nested": [
            {"Set-Cookie": "session=cookie-response", "refreshToken": "refresh-secret"},
            {"client_secret": "client-secret", "api-key": "api-secret", "password": "password-secret"},
            {"request_headers": {"Cookie": "another-cookie"}},
        ],
        "message": "Request failed Authorization: Bearer embedded-secret",
    }
    clean = sanitise_diagnostic_data(data)
    encoded = json.dumps(clean)

    assert clean["provider"] == "MusicBrainz"
    assert clean["Authorization"] == "[REDACTED]"
    assert clean["Cookie"] == "[REDACTED]"
    assert clean["nested"][0]["refreshToken"] == "[REDACTED]"
    assert clean["nested"][2]["request_headers"] == "[REDACTED]"
    assert clean["message"] == "Request failed Authorization: [REDACTED]"
    assert "secret" not in encoded.replace("client_secret", "")
    assert "cookie-response" not in encoded
    assert "another-cookie" not in encoded
    assert data["Authorization"] == "Bearer auth-secret"


def test_sanitisation_converts_typed_trace_values_to_json_without_raw_object_representations() -> None:
    @dataclass(frozen=True)
    class TraceValue:
        field: MetadataField
        values: tuple[str, ...]
        access_token: str

    class Unstructured:
        def __repr__(self) -> str:
            return "raw-secret-object"

    clean = sanitise_diagnostic_data({
        "event": TraceValue(MetadataField.TITLE, ("Opening", "Finale"), "typed-secret"),
        "unstructured": Unstructured(),
    })

    assert clean["event"] == {"field": "title", "values": ["Opening", "Finale"], "access_token": "[REDACTED]"}
    encoded = json.dumps(clean, allow_nan=False)
    assert "typed-secret" not in encoded
    assert "raw-secret-object" not in encoded


def test_nested_diagnostic_error_text_redacts_proxy_and_xml_credentials() -> None:
    data = {"error": ["Proxy refused http://PROXY_USER_SENTINEL:PROXY_PASS_SENTINEL@127.0.0.1:8080",
                      "<error><password>XML_SECRET_SENTINEL</password><status>407</status></error>"]}

    encoded = json.dumps(sanitise_diagnostic_data(data))

    assert "SENTINEL" not in encoded
    assert "127.0.0.1:8080" in encoded
    assert "407" in encoded
