"""A catalogue identity must not pick an arbitrary payload within one response."""

from dataclasses import replace
from itertools import permutations

import pytest

from metadata_polisher.domain.errors import ProviderErrorCode
from metadata_polisher.domain.matching import LocalisedText, ProviderTrack, ReleaseMedium
from metadata_polisher.providers.coordinator import ProviderCoordinator
from tests.unit.providers.test_coordinator import FakeProvider, make_candidate, make_context, make_query


@pytest.mark.parametrize("different_component", ["title", "completeness", "sibling_medium"])
def test_conflicting_duplicate_payloads_fail_the_response_in_either_order(different_component) -> None:
    first = make_candidate("inconsistent", "same-record", title="First title")

    if different_component == "title":
        second = make_candidate("inconsistent", "same-record", title="Another title")
    elif different_component == "completeness":
        second = replace(first, media_complete=False)
    else:
        track = ProviderTrack(1, (LocalisedText("Opening", "en", "Latn"),), (), (), 60)
        disc_one = ReleaseMedium(1, None, (track,))
        disc_two = ReleaseMedium(2, "Original second disc", (track,))
        first = replace(first, media=(disc_one, disc_two))
        second = replace(first, media=(disc_one, replace(disc_two, title="Conflicting second disc")))

    independent = make_candidate("working", "another-record")

    # Checking the whole response before publishing any hit prevents a reversed
    # array from deciding which conflicting version enters ranking or the cache.
    for repeated in permutations((first, second)):
        notifications = []
        providers = (
            FakeProvider("inconsistent", candidates=repeated),
            FakeProvider("working", candidates=(independent,)),
        )
        result = ProviderCoordinator(providers).search_release_queries(
            (make_query(),), make_context(),
            on_call_finished=lambda engine, issue, captured=notifications: captured.append((engine, issue)),
        )

        assert tuple(item.candidate for item in result.candidates) == (independent,)
        assert len(result.failures) == 1
        assert result.failures[0].engine_id == "inconsistent"
        assert result.failures[0].issue.code is ProviderErrorCode.INVALID_RESPONSE
        assert result.failures[0].issue.technical_detail is None
        assert result.summaries[0].successful_queries == 0
        assert result.summaries[0].candidate_count == 0
        assert notifications == [("inconsistent", result.failures[0].issue), ("working", None)]


def test_equal_separately_constructed_repeats_remain_one_successful_candidate() -> None:
    candidate = make_candidate("provider", "same-record")
    equal_copy = replace(candidate, titles=tuple(replace(title) for title in candidate.titles))
    provider = FakeProvider("provider", candidates=(candidate, equal_copy, candidate))

    result = ProviderCoordinator((provider,)).search_releases(make_query(), make_context())

    assert result.failures == ()
    assert tuple(item.candidate for item in result.candidates) == (candidate,)
    assert len(result.candidates[0].provenance) == 1
    assert result.summaries[0].successful_queries == 1
    assert result.summaries[0].candidate_count == 1


def test_separate_search_strategies_keep_the_first_valid_query_payload() -> None:
    primary = make_candidate("provider", "same-record", title="Strict query result")
    broader = make_candidate("provider", "same-record", title="Broader query result")

    class StrategyProvider(FakeProvider):
        def search_releases(self, query, context):
            self.call_log.append(self.engine_id)

            return (primary if len(self.call_log) == 1 else broader,)

    queries = (make_query(), replace(make_query(), local_track_count=0))
    provider = StrategyProvider("provider")
    result = ProviderCoordinator((provider,)).search_release_queries(queries, make_context())

    # The application explicitly orders strict-to-broad strategies. Unlike two
    # conflicting rows in one response, this is a reproducible priority rule.
    assert result.failures == ()
    assert tuple(item.candidate for item in result.candidates) == (primary,)
    assert result.summaries[0].successful_queries == 2
