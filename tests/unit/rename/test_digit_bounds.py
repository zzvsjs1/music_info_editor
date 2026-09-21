"""Reject impractical padding before Python's number formatter allocates it."""

import pytest

from metadata_polisher.domain.metadata import MetadataSnapshot, Position
from metadata_polisher.rename.template import FilenameRenderPolicy, parse_template, render_template


@pytest.mark.parametrize("field", ("minimum_track_digits", "minimum_disc_digits"))
@pytest.mark.parametrize("width", (11, 2_147_483_647))
def test_render_policy_rejects_oversized_width_before_any_render(field, width):
    with pytest.raises(ValueError, match=rf"{field}.*10"):
        FilenameRenderPolicy(**{field: width})


def test_largest_padding_width_keeps_full_manual_position_range():
    policy = FilenameRenderPolicy(minimum_track_digits=10, minimum_disc_digits=10)
    metadata = MetadataSnapshot(track=Position(2_147_483_647), disc=Position(1))

    assert render_template(parse_template("%discnumber%.%tracknumber%"), metadata, policy) == "0000000001.2147483647"
