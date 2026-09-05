"""Single-provider regressions exercise real adapters through offline HTTP."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from metadata_polisher.infrastructure.settings import AppSettings, ProvidersSettings, load_settings, save_settings
from metadata_polisher.providers.base import RequestContext
from metadata_polisher.providers.runtime import ProviderRuntime
from metadata_polisher.session.state import SessionState
from tests.unit.application.test_lookup_service import make_group, make_media_file
from tests.unit.providers.test_runtime import CONTACT, FIXTURES, FakeClock, runtime_with_fixtures


def test_fresh_settings_select_musicbrainz_without_migration_warning(tmp_path: Path) -> None:
    result = load_settings(tmp_path / "settings.json")

    assert result.settings.providers.selected_provider_id == "musicbrainz_direct"
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("musicbrainz_enabled", "vgmdb_enabled", "expected"),
    [(True, True, "musicbrainz_direct"), (True, False, "musicbrainz_direct"),
     (False, True, "vgmdb"), (False, False, None)],
)
def test_legacy_selection_migrates_once_preserving_unrelated_settings(
    tmp_path: Path, musicbrainz_enabled: bool, vgmdb_enabled: bool, expected: str | None,
) -> None:
    path = tmp_path / "settings.json"
    document = {
        "schema_version": 1,
        "providers": {
            "musicbrainz": {"enabled": musicbrainz_enabled, "priority": 1},
            "vgmdb": {"enabled": vgmdb_enabled, "priority": 999},
        },
        "rename": {"enabled": False, "template": "%title%", "minimum_track_digits": 4},
        "backup": {"enabled": True, "directory": "archive"},
        "reports": {"enabled": True, "directory": "reports"},
        "matching": {"preferred_language": "ja"},
    }
    original = json.dumps(document)
    path.write_text(original, encoding="utf-8")

    loaded = load_settings(path)

    assert loaded.settings.providers.selected_provider_id == expected
    assert len(loaded.warnings) == 1
    assert "migrat" in loaded.warnings[0].casefold()
    assert path.read_text(encoding="utf-8") == original
    assert loaded.settings.rename.template == "%title%"
    assert loaded.settings.rename.minimum_track_digits == 4
    assert not loaded.settings.rename.enabled
    assert loaded.settings.backup.enabled and loaded.settings.backup.directory == "archive"
    assert loaded.settings.reports.enabled and loaded.settings.reports.directory == "reports"
    assert loaded.settings.matching.preferred_language == "ja"

    save_settings(path, loaded.settings)
    saved = json.loads(path.read_text(encoding="utf-8"))
    reloaded = load_settings(path)

    assert saved["providers"] == {"selected_provider_id": expected}
    assert reloaded.settings == loaded.settings
    assert reloaded.warnings == ()


@pytest.mark.parametrize("selected", ["musicbrainz_direct", "vgmdb", None])
def test_explicit_selection_outranks_obsolete_enablement(tmp_path: Path, selected: str | None) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "providers": {
            "selected_provider_id": selected,
            "musicbrainz": {"enabled": True, "priority": 999},
            "vgmdb": {"enabled": False, "priority": 1},
        },
    }), encoding="utf-8")

    loaded = load_settings(path)

    assert loaded.settings.providers.selected_provider_id == selected
    assert loaded.warnings == ()


def test_unavailable_explicit_selection_is_preserved_and_reported(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 1, "providers": {"selected_provider_id": "unavailable-adapter"},
    }), encoding="utf-8")

    loaded = load_settings(path)

    assert loaded.settings.providers.selected_provider_id == "unavailable-adapter"
    assert any("provider" in warning.casefold() for warning in loaded.warnings)

    save_settings(path, loaded.settings)
    assert load_settings(path).settings.providers.selected_provider_id == "unavailable-adapter"


@pytest.mark.parametrize("selected", ["musicbrainz_direct", "vgmdb", None])
def test_runtime_configuration_and_search_contact_only_the_selected_provider(selected: str | None) -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()

    try:
        service = runtime.service(
            ProvidersSettings(selected_provider_id=selected),
            musicbrainz_contact=CONTACT if selected == "musicbrainz_direct" else "",
        )
        assert requests == []
        result = service.search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0101", "auto"))
        expected_engines = set() if selected is None else {selected}
        expected_hosts = (
            {"musicbrainz.org"} if selected == "musicbrainz_direct" else {"vgmdb.net"} if selected else set()
        )

        assert {item.candidate.engine_id for item in result.candidates} == expected_engines
        assert {request.url.host for request, _started in requests} == expected_hosts
        assert result.failures == ()
    finally:
        runtime.close()


def test_unknown_selection_cannot_silently_construct_another_provider() -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()

    try:
        with pytest.raises(ValueError, match="provider"):
            runtime.service(ProvidersSettings(selected_provider_id="unavailable-adapter"), musicbrainz_contact=CONTACT)

        assert requests == []
    finally:
        runtime.close()


@pytest.mark.parametrize("outcome", ["empty", "denied", "authentication", "timeout"])
# Count actual mocked requests, because a correct visible error alone would
# not reveal an unwanted request to the unselected catalogue.
def test_selected_provider_failure_or_empty_result_never_falls_back(outcome: str) -> None:
    requests: list[httpx.Request] = []
    clock = FakeClock()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        if outcome == "timeout":
            raise httpx.ReadTimeout("Synthetic bounded timeout", request=request)

        if outcome in {"denied", "authentication"}:
            return httpx.Response(403 if outcome == "denied" else 401)

        return httpx.Response(200, json={"releases": [], "count": 0, "offset": 0})

    runtime = ProviderRuntime(
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        cache=SessionState(root=None).provider_cache,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    try:
        result = runtime.service(
            ProvidersSettings(selected_provider_id="musicbrainz_direct"), musicbrainz_contact=CONTACT,
        ).search_group(make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0102", "auto"))

        assert requests
        assert {request.url.host for request in requests} == {"musicbrainz.org"}
        assert result.candidates == ()
        assert bool(result.failures) is (outcome != "empty")
    finally:
        runtime.close()


@pytest.mark.parametrize("selected", ["musicbrainz_direct", "vgmdb"])
def test_media_loading_and_enrichment_use_the_same_selected_adapter(selected: str) -> None:
    requests: list[httpx.Request] = []
    clock = FakeClock()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        if request.url.host == "musicbrainz.org":
            if request.url.path == "/ws/2/release":
                return httpx.Response(200, json=json.loads(
                    (FIXTURES / "musicbrainz" / "search_release.json").read_text(encoding="utf-8"),
                ))

            detail = json.loads((FIXTURES / "musicbrainz" / "release_detail.json").read_text(encoding="utf-8"))
            detail["id"] = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=detail)

        assert request.url.host == "vgmdb.net"
        fixture = "search_results.html" if request.url.path == "/search" else "album_detail.html"
        html = (FIXTURES / "vgmdb" / fixture).read_text(encoding="utf-8")

        if fixture == "album_detail.html":
            html = html.replace("/album/4242", request.url.path)

        return httpx.Response(200, text=html)

    runtime = ProviderRuntime(
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        cache=SessionState(root=None).provider_cache,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    try:
        service = runtime.service(ProvidersSettings(selected_provider_id=selected), musicbrainz_contact=CONTACT)
        lookup = service.search_and_rank_group(
            make_group(make_media_file("01.flac", title="Opening")), RequestContext("LOOKUP-0103", "auto"),
            per_engine_limit=1,
        )
        assert lookup.release_ranking.entries
        selected_candidate = next(item for item in lookup.lookup_result.candidates if item.candidate.media)
        enriched = service.enrich_selected(selected_candidate, RequestContext("LOOKUP-0104", "auto"))

        assert enriched.candidate is not None
        assert enriched.failures == ()
        assert enriched.candidate.candidate.engine_id == selected
        assert any(request.url.path not in {"/search", "/ws/2/release"} for request in requests)
        assert {request.url.host for request in requests} == (
            {"musicbrainz.org"} if selected == "musicbrainz_direct" else {"vgmdb.net"}
        )
    finally:
        runtime.close()


# Capture before changing Settings to model a queued operation. Its provider
# remains fixed even though the next service uses the newly selected adapter.
def test_reconfiguration_does_not_reroute_a_captured_service_or_old_candidate() -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()
    settings = AppSettings(providers=ProvidersSettings(selected_provider_id="musicbrainz_direct"))

    try:
        captured_service = runtime.service(settings.providers, musicbrainz_contact=CONTACT)
        changed = replace(settings, providers=ProvidersSettings(selected_provider_id="vgmdb"))
        next_service = runtime.service(changed.providers, musicbrainz_contact="")
        assert requests == []

        # The submitted operation owns its original adapter even after the next
        # service has been configured. Its provenance remains attached to it.
        earlier = captured_service.search_group(
            make_group(make_media_file("01.flac")), RequestContext("LOOKUP-0105", "auto"),
        )
        assert {request.url.host for request, _started in requests} == {"musicbrainz.org"}
        old_candidate = earlier.candidates[0]
        old_provenance = old_candidate.provenance
        request_count = len(requests)
        enrichment = next_service.enrich_selected(old_candidate, RequestContext("LOOKUP-0106", "auto"))

        assert enrichment.candidate is None
        assert enrichment.failures
        assert old_candidate.provenance == old_provenance
        assert len(requests) == request_count
    finally:
        runtime.close()


@pytest.mark.parametrize("selected", ["musicbrainz_direct", "vgmdb"])
def test_explicit_connection_tests_reach_only_the_selected_adapter_each_time(selected: str) -> None:
    runtime, _client, _cache, requests = runtime_with_fixtures()

    try:
        for operation_id in ("TEST-0001", "TEST-0002"):
            service = runtime.service(
                ProvidersSettings(selected), musicbrainz_contact=CONTACT, fresh_cache=True,
            )
            assert service.test_connection(RequestContext(operation_id, "auto")) == ()

        assert len(requests) == 2
        assert {request.url.host for request, _started in requests} == (
            {"musicbrainz.org"} if selected == "musicbrainz_direct" else {"vgmdb.net"}
        )
    finally:
        runtime.close()
