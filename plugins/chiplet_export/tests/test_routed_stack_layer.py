# SPDX-License-Identifier: GPL-3.0-or-later
"""routed.source.stack_layer_name: the metal a KiCad layer token names in the
routed document's stack, bottom to top, or a refusal that says why."""

import pytest

from chiplet_export.routed.document import RoutedRefused
from chiplet_export.routed.source import stack_layer_name

STACK = [{"metal": "Metal4"}, {"metal": "Metal5"}, {"metal": "TopMetal1"},
         {"metal": "TopMetal2"}]


@pytest.mark.parametrize("token, metal", [
    ("F.Cu", "TopMetal2"), ("In1.Cu", "TopMetal1"), ("In2.Cu", "Metal5"),
    ("B.Cu", "Metal4"),
])
def test_each_token_names_its_metal(token, metal):
    assert stack_layer_name(STACK, token) == metal


@pytest.mark.parametrize("token, message", [
    ("TopMetal2", "no metal layer for 'TopMetal2' in the routed stack"),
    (None, "no metal layer for None in the routed stack"),
    ("In4.Cu", "the routed stack of 4 metals has no In4.Cu"),
])
def test_a_token_the_stack_does_not_have_is_refused(token, message):
    with pytest.raises(RoutedRefused) as exc:
        stack_layer_name(STACK, token)
    assert str(exc.value) == message
