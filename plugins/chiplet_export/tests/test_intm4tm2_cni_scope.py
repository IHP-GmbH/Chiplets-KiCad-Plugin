# SPDX-License-Identifier: GPL-3.0-or-later
"""Each PDK's PCells run on that PDK's own cni API, in one worker process.

SG13G2 and IntM4TM2 ship their own copy of the pycell4klayout-api under the
same module names, and the worker loads SG13G2 first (the via_stack probe in
GDSGenerator.__init__). Every IntM4TM2 PCell therefore runs inside
_intm4tm2_scope. What that must guarantee, each in a fresh interpreter so the
answer does not depend on test order:

  * the four IntM4TM2 PCells (CuPillarPad, bondpad, cmim, sealring) draw
    the same geometry after an SG13G2 PCell as in a process that never
    loaded SG13G2;
  * an SG13G2 via_stack created after them draws the same geometry as in a
    process that never loaded IntM4TM2, and SG13G2's cni is the one loaded
    after every scope exit;
  * an IntM4TM2 library registered outside the scope is refused at every
    call site, and nothing of it reaches the layout.
"""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("klayout.db")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import hyp_to_gds as h  # noqa: E402

LYP = PLUGIN_ROOT / "tests" / "fixtures" / "intm4tm2_reference.lyp"


def _roots():
    sg13g2 = h._discover_path_var("PDK_ROOT")
    interposer = h._discover_path_var("INTERPOSER_PDK_ROOT")
    interconnect = h._discover_path_var("INTERCONNECT_PDK_ROOT")
    missing = []
    if not sg13g2 or not Path(sg13g2).joinpath(
            *h.GDSGenerator._SG13G2_KLAYOUT_SUBPATH).is_dir():
        missing.append("SG13G2 (PDK_ROOT)")
    if not interposer or not (
            Path(interposer) / "libs.tech" / "klayout" / "python"
            / "intm4tm2_pycell_lib" / "ihp" / "sealring_code.py").is_file():
        missing.append("interposer PDK with the sealring PCell")
    if not interconnect:
        missing.append("interconnect PDK")
    return sg13g2, interposer, missing


SG13G2_ROOT, INTERPOSER_ROOT, MISSING = _roots()
needs_pdks = pytest.mark.skipif(
    bool(MISSING), reason="needs " + ", ".join(MISSING or ["nothing"]))

# One mode per interpreter. Prints one RESULT line of JSON at the end.
CHILD = r'''
import contextlib, hashlib, importlib, io, json, os, sys
plugin, mode, lyp = sys.argv[1:4]
sys.path.insert(0, plugin)
import klayout.db as db
import hyp_to_gds as h

result = {"exits": {}, "cells": {}, "refused": {}}


def cni():
    return getattr(sys.modules.get("cni.dlo"), "__file__", None)


def digest(layout, cell):
    out = {}
    for li in layout.layer_indexes():
        region = db.Region(cell.begin_shapes_rec(li))
        if region.is_empty():
            continue
        info = layout.get_info(li)
        polys = "\n".join(sorted(str(p) for p in region.merged().each()))
        out["%d/%d" % (info.layer, info.datatype)] = hashlib.sha256(
            polys.encode()).hexdigest()
    return out


def portions():
    # Where the loaded cni packages would import a new submodule from.
    return [path for name in ("cni", "pypreprocessor")
            if name in sys.modules for path in sys.modules[name].__path__]


def cni_files():
    # Every cni submodule as an import sees it now, loaded or not.
    api = os.path.dirname(cni())
    files = portions()
    for entry in sorted(os.listdir(api)):
        if entry.endswith(".py"):
            try:
                module = importlib.import_module("cni." + entry[:-3])
            except Exception:
                continue
            files.append(module.__file__)
    return files


def via_after():
    # Parameters no earlier via_stack used, so the library evaluates it now
    # instead of handing back a cached variant.
    layout = db.Layout()
    layout.technology_name = "sg13g2"
    via = layout.create_cell("via_stack", "SG13_dev", {
        "b_layer": "Metal2", "t_layer": "TopMetal1",
        "vn_columns": 3, "vn_rows": 2, "vt1_columns": 2, "vt1_rows": 2})
    return digest(layout, via)


if mode in ("interposer_only", "registered_outside"):
    h.GDSGenerator._check_pcells_available = lambda self: False
if mode == "registered_outside":
    # The interposer's own cni, but loaded before the export and outside the
    # scope, as a session autorun would: not the copy the scope holds.
    python = (h._discover_path_var("INTERPOSER_PDK_ROOT")
              + "/libs.tech/klayout/python")
    sys.path[:0] = [python, python + "/pycell4klayout-api/source/python"]
    import intm4tm2_pycell_lib  # noqa: F401
g = h.GDSGenerator(h.LayerMap(lyp), "TOP", "METRIC", [], None)
g.top_cell.shapes(g.layout.layer(*g.PRBOUNDARY_LAYER)).insert(
    db.DBox(0.0, 0.0, 600.0, 500.0))
result["cni_before"] = cni()
bm = h._import_bump_mirror()
pillars = bm.CuPillarGenerator(enclosure_um=7.5)
bumps = [bm.BumpLocation("U1", "p0", 0.0, 0.0)]
cmim = [{"ref": "C1", "x_um": 300.0, "y_um": 250.0, "w_um": 57.68,
         "l_um": 57.68, "m": 1}]

if mode == "refusal":
    # Registered outside the scope, so on SG13G2's cni: what an earlier
    # import in the same process would leave behind.
    root = h._discover_path_var("INTERPOSER_PDK_ROOT")
    sys.path.insert(0, root + "/libs.tech/klayout/python")
    import intm4tm2_pycell_lib  # noqa: F401
    before = sorted(c.name for c in g.layout.each_cell())
    try:
        g.add_seal_ring()
    except h.SealRingRefused as exc:
        result["refused"]["sealring"] = str(exc)
    result["exits"]["sealring"] = cni()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result["cmim_return"] = g.add_cmim_devices(cmim)
    result["refused"]["cmim"] = err.getvalue()
    result["exits"]["cmim"] = cni()
    try:
        g._create_wire_bond_pad_cell(100.0, 100.0)
    except h.IntM4TM2Refused as exc:
        result["refused"]["bondpad"] = str(exc)
    result["exits"]["bondpad"] = cni()
    try:
        h._add_device_pillars(bm, pillars, "U1", bumps, 44.0)
    except h.IntM4TM2Refused as exc:
        result["refused"]["CuPillarPad"] = str(exc)
    result["exits"]["CuPillarPad"] = cni()
    result["added_cells"] = sorted(
        set(c.name for c in g.layout.each_cell()) - set(before))
    result["pillar_cells"] = sorted(
        c.name for c in pillars.layout.each_cell())
    result["has_seal_ring"] = g.has_seal_ring()
elif mode == "registered_outside":
    try:
        g.add_seal_ring()
    except h.SealRingRefused as exc:
        result["refused"]["sealring"] = str(exc)
    result["has_seal_ring"] = g.has_seal_ring()
elif mode in ("sg13g2_first", "interposer_only"):
    # The flow's own order: pillars, pads, CMIM, then the ring.
    h._add_device_pillars(bm, pillars, "U1", bumps, 44.0)
    result["exits"]["CuPillarPad"] = cni()
    result["cells"]["CuPillarPad"] = digest(
        pillars.layout, pillars.layout.cell("CUPILLARS_U1"))
    pad = g._create_wire_bond_pad_cell(100.0, 100.0)
    result["exits"]["bondpad"] = cni()
    result["cells"]["bondpad"] = digest(g.layout, pad)
    result["cmim_return"] = g.add_cmim_devices(cmim)
    result["exits"]["cmim"] = cni()
    result["cells"]["cmim"] = digest(g.layout, g.layout.cell("CMIM_DEVICES"))
    g.add_seal_ring()
    result["exits"]["sealring"] = cni()
    result["cells"]["sealring"] = digest(
        g.layout, g.layout.cell(g._seal_ring[0]))
    with h._intm4tm2_scope():
        result["scope_portions"] = portions()
    lib = db.Library.library_by_name("IntM4TM2", "intm4tm2")
    result["declared"] = {
        name: type(lib.layout().pcell_declaration(name)).produce.__code__
        .co_filename for name in ("CuPillarPad", "bondpad", "cmim",
                                  "sealring")}
if mode in ("sg13g2_first", "sg13g2_only"):
    result["via"] = via_after()
    result["cni_after_via"] = cni()
    result["cni_files"] = cni_files()
result["modules"] = sorted(name for name in ("sg13g2_pycell_lib",
                                             "intm4tm2_pycell_lib")
                           if name in sys.modules)
print("RESULT " + json.dumps(result))
'''


def _run(mode, tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(PLUGIN_ROOT), mode, str(LYP)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    lines = [line for line in proc.stdout.splitlines()
             if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, (mode, proc.stdout[-3000:],
                                            proc.stderr[-3000:])
    return json.loads(lines[-1][len("RESULT "):])


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cni_scope")
    return {mode: _run(mode, tmp) for mode in (
        "sg13g2_first", "interposer_only", "sg13g2_only", "refusal",
        "registered_outside")}


def _under(path, root):
    root = os.path.realpath(root)
    return os.path.commonpath([root, os.path.realpath(path)]) == root


@pytest.mark.needs_sibling_sources
@needs_pdks
def test_intm4tm2_pcells_draw_the_same_after_an_sg13g2_pcell(runs):
    first, alone = runs["sg13g2_first"], runs["interposer_only"]
    # Not vacuous: SG13G2's cni was loaded before the first IntM4TM2 PCell,
    # and the control never loaded SG13G2 at all.
    assert _under(first["cni_before"], SG13G2_ROOT)
    assert alone["cni_before"] is None
    assert alone["modules"] == ["intm4tm2_pycell_lib"]
    assert first["cmim_return"] == alone["cmim_return"] == [1, []]
    assert sorted(first["cells"]) == ["CuPillarPad", "bondpad", "cmim",
                                      "sealring"]
    for name, layers in first["cells"].items():
        assert layers, name
        assert layers == alone["cells"][name], name
    for run in (first, alone):
        for name, path in run["declared"].items():
            assert _under(path, INTERPOSER_ROOT), (name, path)
        assert len(run["scope_portions"]) == 2
        for path in run["scope_portions"]:
            assert _under(path, INTERPOSER_ROOT), path


@pytest.mark.needs_sibling_sources
@needs_pdks
def test_sg13g2_keeps_its_cni_after_every_intm4tm2_scope(runs):
    first, alone = runs["sg13g2_first"], runs["sg13g2_only"]
    assert first["modules"] == ["intm4tm2_pycell_lib", "sg13g2_pycell_lib"]
    assert alone["modules"] == ["sg13g2_pycell_lib"]
    assert sorted(first["exits"]) == ["CuPillarPad", "bondpad", "cmim",
                                      "sealring"]
    for name, path in first["exits"].items():
        assert path == first["cni_before"], name
    assert first["cni_after_via"] == first["cni_before"]
    # A cni submodule first imported after the scopes, by SG13G2 code, is
    # SG13G2's too: no interposer module or path entry is left behind.
    assert len(first["cni_files"]) > 2
    for path in first["cni_files"]:
        assert _under(path, SG13G2_ROOT), path
    assert first["via"] and first["via"] == alone["via"]


@pytest.mark.needs_sibling_sources
@needs_pdks
def test_a_library_registered_on_sg13g2s_cni_is_refused(runs):
    refusal = runs["refusal"]
    sg13g2_dlo = refusal["cni_before"]
    assert _under(sg13g2_dlo, SG13G2_ROOT)
    assert sorted(refusal["refused"]) == ["CuPillarPad", "bondpad", "cmim",
                                          "sealring"]
    for name, message in refusal["refused"].items():
        assert "IntM4TM2 %s PCell" % name in message, message
        assert sg13g2_dlo in message, message
        assert os.path.realpath(INTERPOSER_ROOT) in message, message
        # The refusal exit restores SG13G2's cni too.
        assert refusal["exits"][name] == sg13g2_dlo, name
    assert refusal["cmim_return"] == [0, ["C1"]]
    assert refusal["added_cells"] == []
    assert refusal["pillar_cells"] == ["TOP"]
    assert refusal["has_seal_ring"] is False


@pytest.mark.needs_sibling_sources
@needs_pdks
def test_a_library_registered_before_the_export_is_refused(runs):
    # Its declaration is the interposer's, but its cni is not the copy the
    # scope holds, so the two would mix at the first call-time import.
    outside = runs["registered_outside"]
    interposer_dlo = outside["cni_before"]
    assert _under(interposer_dlo, INTERPOSER_ROOT)
    message = outside["refused"]["sealring"]
    assert "IntM4TM2 sealring PCell" in message
    assert "registered with %s" % interposer_dlo in message, message
    assert outside["has_seal_ring"] is False


def test_the_scope_swaps_every_name_both_pdks_share(monkeypatch):
    # Names only one side loads: a preprocessed PCell module, registered
    # top-level as '<pcell>_code', and a cni submodule first imported inside.
    monkeypatch.setattr(h, "_intm4tm2_modules", {})

    def module(name, path):
        made = types.ModuleType(name)
        made.__file__ = path
        return made

    outside = module("probe_code", "/sg13g2/probe_code_pre.py")
    inside = module("probe_code", "/intm4tm2/probe_code_pre.py")
    plain = module("plain_code", "/elsewhere/plain_code.py")
    submodule = module("cni._scope_probe", "/intm4tm2/cni/_scope_probe.py")
    monkeypatch.setitem(sys.modules, "probe_code", outside)
    monkeypatch.setitem(sys.modules, "plain_code", plain)
    path = list(sys.path)
    with h._intm4tm2_scope():
        assert "probe_code" not in sys.modules
        assert sys.modules["plain_code"] is plain
        sys.modules["probe_code"] = inside
        sys.modules["cni._scope_probe"] = submodule
    assert sys.modules["probe_code"] is outside
    assert "cni._scope_probe" not in sys.modules
    assert sys.path == path
    with h._intm4tm2_scope():
        assert sys.modules["probe_code"] is inside
        assert sys.modules["cni._scope_probe"] is submodule
    assert sys.modules["probe_code"] is outside
    assert "cni._scope_probe" not in sys.modules
