"""Named release identities retain the existing tuple boundary behaviour."""

import json

from metadata_polisher.domain.release_identity import ReleaseMediumIdentity


def test_named_identity_retains_tuple_order_hash_and_json_shape() -> None:
    identity = ReleaseMediumIdentity("engine", "catalogue", "release-9", 2)
    legacy = ("engine", "catalogue", "release-9", 2)

    assert identity.engine_id == "engine"
    assert identity.source_id == "catalogue"
    assert identity.release_id == "release-9"
    assert identity.medium_index == 2
    assert identity.release_identity == legacy[:3]
    assert identity == legacy
    assert hash(identity) == hash(legacy)
    assert {identity: "selected"}[legacy] == "selected"
    assert json.dumps(identity) == json.dumps(legacy)


def test_identity_sorting_keeps_catalogue_components_before_medium_index() -> None:
    identities = (
        ReleaseMediumIdentity("engine", "source-b", "release-a", 0),
        ReleaseMediumIdentity("engine", "source-a", "release-b", 0),
        ReleaseMediumIdentity("engine", "source-a", "release-a", 2),
        ReleaseMediumIdentity("engine", "source-a", "release-a", 0),
    )

    assert sorted(identities) == [
        ("engine", "source-a", "release-a", 0),
        ("engine", "source-a", "release-a", 2),
        ("engine", "source-a", "release-b", 0),
        ("engine", "source-b", "release-a", 0),
    ]
