# SPDX-License-Identifier: GPL-3.0-or-later
"""The mount plane is the interposer PDK's, read on every finalize.

The plane dies mount on is ``beol.attachment_surface_z_um`` in
``$INTERPOSER_PDK_ROOT/libs.tech/intm4tm2_stack.json``. There is no value
written from memory: without the PDK, or with a descriptor that does not
give the key as a number, the finalizer raises and the worker refuses
before anything is written; both name INTERPOSER_PDK_ROOT and the key. A
root without the descriptor is not an IntM4TM2 tree, and the discovery refuses
it first with CARRIER_UNSUPPORTED_ROOT.

Every z the finalizer derives from the plane is the exact decimal sum of the
plane and the heights as they are written, emitted as the shortest text: a
height with five decimals keeps them, and 10.8103 + 7.77777 is 18.58807,
never the 18.588070000000002 float addition gives.
"""
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

db = pytest.importorskip("klayout.db")
yaml = pytest.importorskip("yaml")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

from .conftest import STACK_DESCRIPTOR, make_pdk_root  # noqa: E402
from .test_die_outline_finalize import (  # noqa: E402
    OUTLINE, _board, _chiplet, _die, _die_gds, _run_worker, _tree_state,
    _update,
)

KEY = "beol.attachment_surface_z_um"


def _root_with(tmp_path, monkeypatch, text):
    """INTERPOSER_PDK_ROOT set to a root whose descriptor is ``text``."""
    root = make_pdk_root(tmp_path, None)
    (root / "libs.tech" / "intm4tm2_stack.json").write_text(
        text, encoding="utf-8")
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    return root


def _descriptor(**beol):
    data = json.loads(STACK_DESCRIPTOR.read_text(encoding="utf-8"))
    data["beol"] = beol
    return json.dumps(data)


def _stacked(tmp_path, heights):
    """A .chiplet whose one die U1 sits on a stack of ``heights``, given in
    the file as YAML numbers, so the finalizer keeps the stack it reads."""
    u1 = _die_gds(tmp_path / "u1.gds", (0.0, 0.0, 100.0, 100.0))
    path = Path(_chiplet(tmp_path / "t.chiplet",
                         [("U1", {"connection": "s", "layout": u1})]))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["connection_stacks"] = {"s": {"layers": [
        {"name": "L%d" % i, "height": float(text)}
        for i, text in enumerate(heights)]}}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

def test_the_plane_is_the_descriptor_value_as_written(attach_frame_pdk):
    frame = h.read_attach_frame()
    assert frame.z_um == Decimal("10.8103")
    assert frame.source == str(attach_frame_pdk / "libs.tech"
                               / "intm4tm2_stack.json")
    assert repr(frame.at()) == "10.8103"


def test_an_integer_plane_is_read(tmp_path, monkeypatch):
    _root_with(tmp_path, monkeypatch, _descriptor(attachment_surface_z_um=11))
    assert h.read_attach_frame().z_um == Decimal(11)


def test_without_the_pdk_the_reader_refuses(monkeypatch):
    monkeypatch.setattr(h, "_discover_path_var", lambda name: None)
    with pytest.raises(h.AttachFrameRefused,
                       match=r"interposer PDK was not found \(set "
                             r"INTERPOSER_PDK_ROOT .*beol\.attachment_surface"
                             r"_z_um in libs\.tech/intm4tm2_stack\.json"):
        h.read_attach_frame()


def test_a_root_without_the_descriptor_is_not_an_intm4tm2_tree(
        tmp_path, monkeypatch):
    # The descriptor is the marker of an IntM4TM2 tree, so its absence is
    # the carrier root's refusal, which ends the run.
    root = make_pdk_root(tmp_path, None)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    with pytest.raises(SystemExit,
                       match=r"^CARRIER_UNSUPPORTED_ROOT: \$INTERPOSER_PDK_ROOT "
                             r"is %s, which has no libs\.tech/intm4tm2_stack"
                             r"\.json," % re.escape(str(root))):
        h.read_attach_frame()


@pytest.mark.parametrize("text,found", [
    (_descriptor(), "None"),
    (_descriptor(attachment_surface_z_um="10.8103"), "'10.8103'"),
    (_descriptor(attachment_surface_z_um=True), "True"),
    (_descriptor(attachment_surface_z_um=None), "None"),
    (_descriptor(attachment_surface_z_um=[10.8103]), r"\[Decimal\('10.8103'\)\]"),
    ('{"beol": {"attachment_surface_z_um": NaN}}', "nan"),
    ('{"beol": {"attachment_surface_z_um": Infinity}}', "inf"),
    ('{"beol": 10.8103}', "None"),
    ('[10.8103]', "None"),
], ids=["missing", "string", "bool", "null", "list", "nan", "infinity",
        "beol-not-a-mapping", "not-a-mapping"])
def test_a_descriptor_without_the_number_refuses(tmp_path, monkeypatch, text,
                                                 found):
    root = _root_with(tmp_path, monkeypatch, text)
    with pytest.raises(h.AttachFrameRefused,
                       match=r"intm4tm2_stack\.json has no "
                             r"beol\.attachment_surface_z_um that is a "
                             r"number of um \(found %s\), under "
                             r"INTERPOSER_PDK_ROOT=%s$" % (found, root)):
        h.read_attach_frame()


def test_a_descriptor_that_is_not_json_refuses(tmp_path, monkeypatch):
    root = _root_with(tmp_path, monkeypatch, '{"beol": ')
    with pytest.raises(h.AttachFrameRefused,
                       match=r"^cannot read beol\.attachment_surface_z_um in "
                             r"libs\.tech/intm4tm2_stack\.json under "
                             r"INTERPOSER_PDK_ROOT=%s: Expecting value" % root):
        h.read_attach_frame()


def test_the_refusal_is_no_lookup_error():
    """Like DieOutlineRefused: no broad handler may absorb it."""
    assert not issubclass(h.AttachFrameRefused,
                          (ValueError, KeyError, OSError, LookupError))


# ---------------------------------------------------------------------------
# z on the plane: the exact decimal sum, as the shortest text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heights,z", [
    ((28.0, 16.0), "54.8103"),          # cupillar_opt1
    ((18.00005,), "28.81035"),           # five decimals, never 28.8103
    ((7.77777,), "18.58807"),            # never 18.588070000000002
    ((1.1, 2.2), "14.1103"),             # added in turn, 14.110299999999999
    ((), "10.8103"),
    ((5,), "15.8103"),
])
def test_z_is_the_exact_sum_of_the_values_as_written(heights, z):
    frame = h.AttachFrame(Decimal("10.8103"), "descriptor")
    assert repr(frame.at(*heights)) == z


def test_a_plane_a_caller_gives_is_taken_as_written():
    frame = h.AttachFrame.given(13.83)
    assert frame.z_um == Decimal("13.83") and frame.source == "caller"
    assert repr(frame.at(28.0, 16.0)) == "57.83"


# ---------------------------------------------------------------------------
# The finalizer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heights,z,height", [
    (["18.00005"], "28.81035", "18.00005"),
    (["7.77777"], "18.58807", "7.77777"),
])
def test_the_finalizer_writes_the_plane_and_the_exact_die_z(
        tmp_path, attach_frame_pdk, capsys, heights, z, height):
    chiplet = _stacked(tmp_path, heights)
    assert _update(chiplet, die_outline_exempt=["U1"]) is True
    text = Path(chiplet).read_text(encoding="utf-8")
    assert "  attachment_surface_z: 10.8103\n" in text
    assert "    z: %s\n" % z in text
    assert repr(_die(chiplet, "U1")["position"]["z"]) == z
    assert "U1: z = 10.8103 + %s = %s um (connection: s)" % (height, z) in (
        capsys.readouterr().out)


def test_a_die_without_a_stack_sits_on_the_plane(tmp_path, attach_frame_pdk):
    u1 = _die_gds(tmp_path / "u1.gds", (0.0, 0.0, 100.0, 100.0))
    chiplet = _chiplet(tmp_path / "t.chiplet", [("U1", {"layout": u1})])
    assert _update(chiplet, die_outline_exempt=["U1"]) is True
    assert repr(_die(chiplet, "U1")["position"]["z"]) == "10.8103"


def test_the_finalizer_without_the_pdk_raises_and_writes_nothing(
        tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_discover_path_var", lambda name: None)
    chiplet = _stacked(tmp_path, ["18.00005"])
    before = Path(chiplet).read_bytes()
    with pytest.raises(h.AttachFrameRefused, match="INTERPOSER_PDK_ROOT"):
        _update(chiplet, die_outline_exempt=["U1"])
    assert Path(chiplet).read_bytes() == before


def test_a_caller_plane_needs_no_pdk(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "_discover_path_var", lambda name: None)
    chiplet = _stacked(tmp_path, ["18.00005"])
    assert _update(chiplet, die_outline_exempt=["U1"],
                   attachment_surface_z=20.0) is True
    assert repr(_die(chiplet, "U1")["position"]["z"]) == "38.00005"


def test_the_entry_point_reports_a_raised_refusal(monkeypatch, capsys):
    def refuse():
        raise h.AttachFrameRefused("a cause naming INTERPOSER_PDK_ROOT")
    monkeypatch.setattr(h, "main", refuse)
    assert h._main_reporting_refusals() == 1
    assert ("Attach frame refused: a cause naming INTERPOSER_PDK_ROOT"
            in capsys.readouterr().err)


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def test_the_worker_reads_the_plane_and_names_it(tmp_path, attach_frame_pdk):
    hyp, _die_path, out, chiplet = _board(tmp_path)
    result = _run_worker(tmp_path, hyp, out, chiplet,
                         ["--die-outline-layers", "U1=%d/%d" % OUTLINE])
    assert result.returncode == 0, result.stderr
    assert "Mount plane: %s = 10.8103 um (%s)" % (
        KEY, attach_frame_pdk / "libs.tech" / "intm4tm2_stack.json") in (
        result.stdout)
    data = yaml.safe_load(Path(chiplet).read_text(encoding="utf-8"))
    interposer = next(c for c in data["components"]
                      if c["type"] == "interposer")
    assert interposer["attachment_surface_z"] == 10.8103


def test_the_worker_refuses_before_writing_anything(tmp_path, monkeypatch):
    hyp, _die_path, out, chiplet = _board(tmp_path)
    root = make_pdk_root(tmp_path / "pdk", None)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    before = _tree_state(str(tmp_path))
    result = _run_worker(tmp_path, hyp, out, chiplet,
                         ["--die-outline-layers", "U1=%d/%d" % OUTLINE])
    assert result.returncode == 1, result.stdout
    assert _tree_state(str(tmp_path)) == before
    assert result.stderr.startswith(
        "CARRIER_UNSUPPORTED_ROOT: $INTERPOSER_PDK_ROOT is %s, which has no "
        "libs.tech/intm4tm2_stack.json," % root)


def test_the_worker_without_a_chiplet_reads_no_plane(tmp_path, monkeypatch):
    """Nothing is placed on the plane without a .chiplet to finalize."""
    hyp, _die_path, out, _none = _board(tmp_path, chiplet=False)
    root = make_pdk_root(tmp_path / "pdk", None)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(root))
    monkeypatch.setattr(h, "_discover_path_var", lambda name: (
        str(root) if name == "INTERPOSER_PDK_ROOT" else None))
    assert h.convert_hyp_to_gds(
        hyp_path=hyp, output_path=str(out / "layout" / "demo_interposer.gds"),
        lyp_path=str(PLUGIN_ROOT / "tests" / "fixtures"
                     / "intm4tm2_reference.lyp")) is True
