# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic checks for observation semantics, using the pcbnew getter surface."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board_snapshot import SnapshotError, _finite, observe_board


PCBNEW = SimpleNamespace(
    PCB_IU_PER_MM=1_000_000,
    PAD_ATTRIB_SMD=1, PADSTACK=SimpleNamespace(MODE_NORMAL=0),
    PAD_SHAPE_CIRCLE=0, PAD_SHAPE_RECT=1, PAD_SHAPE_OVAL=2,
    SHAPE_T_SEGMENT=0, SHAPE_T_RECT=1, SHAPE_T_ARC=2, SHAPE_T_CIRCLE=3,
    Edge_Cuts=44,
)


def vector(x, y):
    return SimpleNamespace(x=x, y=y)


def field(name, text):
    return Mock(GetName=Mock(return_value=name), GetText=Mock(return_value=text))


def pad(number="VDD", net_code=1, net_name="VDD", x=12_000_000, y=19_000_000):
    return Mock(
        GetNumber=Mock(return_value=number),
        GetNetCode=Mock(return_value=net_code),
        GetNetname=Mock(return_value=net_name),
        GetPosition=Mock(return_value=vector(x, y)),
        GetSize=Mock(return_value=vector(50_000, 60_000)),
        GetOrientation=Mock(return_value=SimpleNamespace(AsDegrees=lambda: 90)),
        GetShape=Mock(return_value=PCBNEW.PAD_SHAPE_RECT),
        GetAttribute=Mock(return_value=PCBNEW.PAD_ATTRIB_SMD),
        Padstack=Mock(return_value=SimpleNamespace(Mode=lambda: 0)),
        GetDrillSize=Mock(return_value=vector(0, 0)),
        GetOffset=Mock(return_value=vector(0, 0)),
        GetLayerSet=Mock(return_value=SimpleNamespace(Seq=lambda: [0, 2])),
    )


def footprint(pads=None):
    return Mock(
        GetReference=Mock(return_value="U1"),
        m_Uuid=SimpleNamespace(AsString=lambda: "footprint-uuid"),
        GetPosition=Mock(return_value=vector(10_000_000, 20_000_000)),
        GetOrientation=Mock(return_value=SimpleNamespace(AsDegrees=lambda: 90)),
        GetLayer=Mock(return_value=0),
        GetFields=Mock(return_value=[field("Reference", "U1"),
                                    field("GDS_FILE", " ${LAYOUT}/die.gds "),
                                    field("ORIENTATION", "")]),
        Pads=Mock(return_value=[pad()] if pads is None else pads),
        GraphicalItems=Mock(return_value=[]),
    )


def drawing(shape=0, layer=44):
    return Mock(
        GetClass=Mock(return_value="PCB_SHAPE"),
        GetLayer=Mock(return_value=layer),
        GetShape=Mock(return_value=shape),
        GetWidth=Mock(return_value=100_000),
        GetStart=Mock(return_value=vector(-1_000_000, -2_000_000)),
        GetEnd=Mock(return_value=vector(3_000_000, 4_000_000)),
        GetArcMid=Mock(return_value=vector(2_000_000, 0)),
        GetCenter=Mock(return_value=vector(1_000_000, 2_000_000)),
        GetRadius=Mock(return_value=500_000),
    )


def board(footprints=None, drawings=None):
    names = {0: "F.Cu", 2: "In1.Cu", 31: "B.Cu", 44: "Edge.Cuts"}
    return Mock(
        GetFootprints=Mock(return_value=[footprint()] if footprints is None
                           else footprints),
        GetDrawings=Mock(return_value=[] if drawings is None else drawings),
        GetLayerName=Mock(side_effect=names.__getitem__),
        GetNetsByNetcode=Mock(return_value={
            code: Mock(GetNetname=Mock(return_value=name))
            for code, name in [(0, ""), (1, "VDD"), (2, "single")]}),
    )


class BoardSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.bindings = patch.dict(sys.modules, {"pcbnew": PCBNEW})
        self.bindings.start()
        self.addCleanup(self.bindings.stop)
        # Other plugin tests may have loaded this module with a different
        # pcbnew stand-in. Keep their module state intact outside each test.
        from writers import chiplet_writer
        self.writer_binding = patch.object(chiplet_writer, "pcbnew", PCBNEW)
        self.writer_binding.start()
        self.addCleanup(self.writer_binding.stop)

    def test_preserves_global_pose_and_raw_fields_without_die_interpretation(self):
        fp = footprint()
        fp.GetLayer.return_value = 31
        observed = observe_board(board([fp]))
        self.assertEqual(observed["format"], "chiplet_kicad.board_observation")
        self.assertEqual((observed["version"], observed["units"], observed["frame"]),
                         (1, "mm", "kicad_y_down"))
        result = observed["footprints"][0]
        self.assertEqual(result["at_mm"], [10, 20])
        self.assertEqual(result["layer"], "B.Cu")
        self.assertEqual(result["rotation_deg"], 90)
        self.assertEqual(result["properties"]["ORIENTATION"], "")
        self.assertEqual(result["properties"]["GDS_FILE"], " ${LAYOUT}/die.gds ")
        self.assertEqual(result["pads"][0], {
            "number": "VDD", "uuid": None, "at_mm": [12, 19],
            "size_mm": [0.05, 0.06], "rotation_deg": 90, "shape": "rect",
            "layers": ["F.Cu", "In1.Cu"], "net_code": 1, "net_name": "VDD",
        })
        # The raw rotated global position must not be rotated or recentered.
        self.assertNotIn("pad_index", result["pads"][0])

    def test_repeated_numbers_unassigned_pads_and_singletons_are_not_collapsed(self):
        pads = [pad(), pad(x=11_000_000), pad("", 0, ""), pad("SOLO", 2, "single")]
        result = observe_board(board([footprint(pads)]))
        observed = result["footprints"][0]["pads"]
        self.assertEqual(len(observed), 4)
        self.assertEqual(sum(p["number"] == "VDD" for p in observed), 2)
        self.assertIn((0, ""), [(p["net_code"], p["net_name"]) for p in observed])
        self.assertIn({"code": 2, "name": "single"}, result["nets"])

    def test_presentation_is_stable_under_api_enumeration_and_runtime_pad_uuids(self):
        fp = footprint([pad(x=1), pad(x=2)])
        b = board([fp], [drawing(), drawing(1)])
        first = json.dumps(observe_board(b), sort_keys=True)
        fp.Pads.return_value.reverse()
        b.GetDrawings.return_value.reverse()
        for item in fp.Pads.return_value:
            item.m_Uuid = SimpleNamespace(AsString=lambda: "new-random-runtime-uuid")
        self.assertEqual(first, json.dumps(observe_board(b), sort_keys=True))

    def test_unknown_net_code_and_mismatched_net_name_refuse(self):
        for code, name in [(100, "unknown"), (1, "VSS"), (0, "VDD")]:
            with self.subTest(code=code, name=name), self.assertRaisesRegex(
                    SnapshotError, "inconsistent net"):
                observe_board(board([footprint([pad(net_code=code, net_name=name)])]))

    def test_duplicate_properties_refuse_instead_of_overwriting(self):
        fp = footprint()
        fp.GetFields.return_value = [field("GDS_FILE", "a"), field("GDS_FILE", "b")]
        with self.assertRaisesRegex(SnapshotError, "Duplicate footprint property"):
            observe_board(board([fp]))

    def test_unreadable_required_getter_is_not_treated_as_missing_data(self):
        fp = footprint()
        fp.GetFields.side_effect = AttributeError("GetFields unavailable")
        with self.assertRaisesRegex(AttributeError, "GetFields unavailable"):
            observe_board(board([fp]))

    def test_non_finite_geometry_refuses(self):
        for value in [float("nan"), float("inf"), -float("inf")]:
            with self.subTest(value=value), self.assertRaises(SnapshotError):
                _finite(value)
        fp = footprint()
        fp.GetOrientation.return_value = SimpleNamespace(AsDegrees=lambda: float("nan"))
        with self.assertRaises(SnapshotError):
            observe_board(board([fp]))

    def test_supported_outline_primitives_preserve_geometry(self):
        observed = observe_board(board([], [drawing(kind) for kind in range(4)]))
        shapes = {row["shape"]: row for row in observed["outline"]}
        for kind in ["segment", "rect"]:
            self.assertEqual(shapes[kind], {"shape": kind, "width_mm": 0.1,
                                           "start_mm": [-1, -2], "end_mm": [3, 4]})
        self.assertEqual(shapes["arc"], {"shape": "arc", "width_mm": 0.1,
                                       "start_mm": [-1, -2], "end_mm": [3, 4],
                                       "mid_mm": [2, 0]})
        self.assertEqual(shapes["circle"], {"shape": "circle", "width_mm": 0.1,
                                          "center_mm": [1, 2], "radius_mm": 0.5})

    def test_footprint_edge_cuts_are_included_and_other_layers_ignored(self):
        fp = footprint([])
        fp.GraphicalItems.return_value = [drawing(2), drawing(99, layer=0)]
        observed = observe_board(board([fp], [drawing()]))
        self.assertEqual({row["shape"] for row in observed["outline"]}, {"arc", "segment"})

    def test_unsupported_outline_shapes_and_items_refuse(self):
        for kind in [4, 5, -1]:
            with self.subTest(kind=kind), self.assertRaisesRegex(
                    SnapshotError, "Unsupported Edge.Cuts shape"):
                observe_board(board([], [drawing(kind)]))
        item = drawing()
        item.GetClass.return_value = "PCB_TEXT"
        with self.assertRaisesRegex(SnapshotError, "Unsupported Edge.Cuts item"):
            observe_board(board([], [item]))

    def test_supported_pad_shapes_and_unsupported_parameterized_shapes(self):
        for kind, name in [(0, "circle"), (1, "rect"), (2, "oval")]:
            p = pad()
            p.GetShape.return_value = kind
            self.assertEqual(observe_board(board([footprint([p])]))[
                "footprints"][0]["pads"][0]["shape"], name)
        for kind in [3, 4, 5, 6, 100]:
            p = pad()
            p.GetShape.return_value = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(
                    SnapshotError, "Unsupported pad shape"):
                observe_board(board([footprint([p])]))

    def test_no_mutators_or_exporters_are_called(self):
        fp = footprint()
        b = board([fp])
        observe_board(b)
        for obj in [b, fp, *fp.Pads.return_value]:
            for call in obj.mock_calls:
                method = call[0].split(".")[-1]
                self.assertFalse(method.startswith(("Set", "Save", "Build", "Export")),
                                 str(call))

    def test_non_smd_pads_refuse_even_with_supported_shape_and_layers(self):
        for attribute in [0, 2, 3]:
            p = pad()
            p.GetAttribute.return_value = attribute
            with self.subTest(attribute=attribute), self.assertRaisesRegex(
                    SnapshotError, "U1 pad 'VDD': only SMD"):
                observe_board(board([footprint([p])]))

    def test_drill_and_copper_offset_refuse_in_either_axis(self):
        for method, message in [("GetDrillSize", "drilled pads"),
                                ("GetOffset", "copper offset")]:
            for x, y in [(1, 0), (0, 1), (-1, 0), (0, -1)]:
                p = pad()
                getattr(p, method).return_value = vector(x, y)
                with self.subTest(method=method, x=x, y=y), self.assertRaisesRegex(
                        SnapshotError, "U1 pad 'VDD': " + message):
                    observe_board(board([footprint([p])]))

    def test_nonuniform_stack_refuses_before_using_front_layer_only_shape(self):
        for mode in [1, 2, 100]:
            p = pad()
            p.Padstack.return_value = SimpleNamespace(Mode=lambda: mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(
                    SnapshotError, "U1 pad 'VDD': only uniform NORMAL"):
                observe_board(board([footprint([p])]))
            p.GetShape.assert_not_called()

    def test_missing_pad_inspection_api_refuses_with_context(self):
        for method in ["GetAttribute", "Padstack", "GetDrillSize", "GetOffset"]:
            p = pad()
            delattr(p, method)
            with self.subTest(method=method), self.assertRaisesRegex(
                    SnapshotError, "U1 pad 'VDD': cannot inspect " + method):
                observe_board(board([footprint([p])]))
        p = pad()
        p.Padstack.return_value = SimpleNamespace()
        with self.assertRaisesRegex(SnapshotError, r"cannot inspect Padstack\(\).Mode"):
            observe_board(board([footprint([p])]))

    def test_missing_padstack_enum_and_failed_native_getter_refuse(self):
        with patch.object(PCBNEW, "PADSTACK", None):
            with self.assertRaisesRegex(SnapshotError, "cannot inspect pcbnew.PADSTACK"):
                observe_board(board())
        p = pad()
        p.GetOffset.side_effect = RuntimeError("binding failure")
        with self.assertRaisesRegex(SnapshotError, "U1 pad 'VDD'.*GetOffset.*binding failure"):
            observe_board(board([footprint([p])]))

    def test_nonfinite_hidden_pad_parameters_refuse(self):
        for method in ["GetDrillSize", "GetOffset"]:
            p = pad()
            getattr(p, method).return_value = vector(float("nan"), 0)
            with self.subTest(method=method), self.assertRaisesRegex(
                    SnapshotError, "cannot inspect " + method):
                observe_board(board([footprint([p])]))


if __name__ == "__main__":
    unittest.main()
