# SPDX-License-Identifier: GPL-3.0-or-later
"""The routed geometry's drawn vias against the engine's via table.

On the routed path the worker draws the document's cut arrays; before it
draws anything it holds each one against the table it would size the via
from (the built-in table, or --tech-json's), compared at the layout's dbu:
cut, enclosure, pitch where the document states one, and the cut count the
engine gives for the pad. A mismatch is refused, naming the via type, and
nothing is written. So is a --tech-json that is missing, unreadable or short
of a key, and a document of another major version.

typical_03 draws 22 single TopVia2 cuts with no pitch; the two-cut variant
below gives the pitch and cut count comparisons an operand.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("klayout.db")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hyp_to_gds as _h  # noqa: E402

from .routed_t03_stage import IO_PADS, stage  # noqa: E402

LYP_PATH = Path(_h._find_default_lyp())
TV2 = _h.GDSGenerator._DEFAULT_VIA_PARAMS["TV2"]


@pytest.fixture
def generator():
    if not LYP_PATH.is_file():
        pytest.skip("interposer LYP not found: %s" % LYP_PATH)
    return _h.GDSGenerator(_h.LayerMap(str(LYP_PATH)), "TOP", "METRIC", [], None)


def _drawn(tmp_path, edit=None):
    model = _h.RoutedGeometryParser(str(stage(tmp_path, edit)), str(IO_PADS))
    model.parse()
    return model.drawn_vias


def _first_drawn(document):
    return next(net for net in document["nets"] if net["drawn"])["drawn"][0]


def _two_cuts(pitch=None, k=2):
    """The first drawn via as a two-cut TopVia2 array on the pad the table
    gives for two cuts."""
    pad = 2 * TV2["enc"] + 2 * TV2["size"] + TV2["sep"]

    def edit(document):
        drawn = _first_drawn(document)
        drawn.update(pad=[pad, pad], k=k,
                     pitch=TV2["size"] + TV2["sep"] if pitch is None else pitch)
    return edit


def test_document_vias_match_engine_table(tmp_path, generator):
    """Source: the built-in table (the export passes no --tech-json)."""
    drawn_vias = _drawn(tmp_path)
    assert len(drawn_vias) == 22
    for _net, drawn in drawn_vias:
        assert (drawn["bottom_layer"], drawn["top_layer"]) == ("TopMetal1", "TopMetal2")
        assert round(drawn["cut"] / 0.001) == round(TV2["size"] / 0.001)
        assert round(drawn["enclosure"] / 0.001) == round(TV2["enc"] / 0.001)
        assert generator._calculate_via_array(max(drawn["pad"]), "TV2") == drawn["k"]
    _h.check_routed_via_table(generator, drawn_vias)


def test_a_two_cut_array_from_the_table_passes(tmp_path, generator):
    _h.check_routed_via_table(generator, _drawn(tmp_path, _two_cuts()))


@pytest.mark.parametrize("edit, what", [
    (_two_cuts(pitch=TV2["size"] + TV2["sep"] + 0.01), "pitch"),
    (_two_cuts(k=3), "cuts"),
], ids=["pitch", "count"])
def test_a_two_cut_array_off_the_table_is_refused(tmp_path, generator, edit, what):
    with pytest.raises(_h.ViaTableRefused) as exc:
        _h.check_routed_via_table(generator, _drawn(tmp_path, edit))
    assert "TV2" in str(exc.value) and what in str(exc.value)


def test_a_pitch_on_one_cut_is_refused_before_the_table(tmp_path):
    """The schema gives a single cut no pitch, so the reader refuses one
    before the table is consulted."""
    def edit(document):
        _first_drawn(document)["pitch"] = 2.5
    with pytest.raises(_h.RoutedGeometryRefused) as exc:
        _drawn(tmp_path, edit)
    assert "draws one cut and states a pitch" in str(exc.value)


@pytest.mark.parametrize("key, value", [("cut", 0.8), ("enclosure", 0.6)])
def test_a_cut_or_enclosure_off_the_table_is_refused(tmp_path, generator, key, value):
    def edit(document):
        _first_drawn(document)[key] = value
    with pytest.raises(_h.ViaTableRefused) as exc:
        _h.check_routed_via_table(generator, _drawn(tmp_path, edit))
    assert "TV2" in str(exc.value) and key in str(exc.value)


def test_a_cut_the_table_does_not_hold_is_refused(generator):
    drawn = {"via": "Via3_C", "x": 1.0, "y": 2.0, "bottom_layer": "Metal3",
             "top_layer": "Metal4", "cut": 0.19, "enclosure": 0.05,
             "pitch": None, "k": 1, "pad": [0.29, 0.29]}
    with pytest.raises(_h.ViaTableRefused) as exc:
        _h.check_routed_via_table(generator, [("n", drawn)])
    assert "no via table entry for Metal3 to Metal4" in str(exc.value)


def test_another_major_version_is_refused(tmp_path):
    def edit(document):
        document["version"] = "2.0"
    with pytest.raises(_h.RoutedGeometryRefused) as exc:
        _drawn(tmp_path, edit)
    assert "2.0" in str(exc.value)


def _tech(tmp_path, rules):
    path = tmp_path / "tech.json"
    path.write_text(json.dumps({"rules": rules}))
    return str(path)


FULL_RULES = {"Vn_a": 0.19, "Vn_b": 0.22, "Vn_c1": 0.05, "TV1_a": 0.42,
              "TV1_b": 0.42, "TV1_d": 0.42, "TV2_a": 0.9, "TV2_b": 1.06, "TV2_d": 0.5}


def test_a_tech_json_gives_the_table(tmp_path):
    table = _h.load_via_table(_tech(tmp_path, FULL_RULES))
    assert table["TV2"] == {"size": 0.9, "sep": 1.06, "enc": 0.5}


@pytest.mark.parametrize("case", ["missing key", "unreadable", "missing file"])
def test_a_tech_json_that_cannot_give_the_table_is_refused(tmp_path, case):
    if case == "missing key":
        path = _tech(tmp_path, {k: v for k, v in FULL_RULES.items() if k != "TV2_b"})
        expected = "TV2_b"
    elif case == "unreadable":
        path = tmp_path / "tech.json"
        path.write_text("{not json")
        expected = "cannot read"
    else:
        path = tmp_path / "absent.json"
        expected = "cannot read"
    with pytest.raises(_h.ViaTableRefused) as exc:
        _h.load_via_table(str(path))
    assert expected in str(exc.value)


def _convert(tmp_path, document, **kwargs):
    """The worker on the routed geometry alone: no io pads, dies or .chiplet,
    so nothing but the refusal under test stands before the drawing."""
    out = tmp_path / "out" / "interposer.gds"
    ok = _h.convert_hyp_to_gds("", str(out), str(LYP_PATH),
                               routed_geometry_path=str(document), **kwargs)
    return ok, out


@pytest.mark.parametrize("case", ["version", "via table", "tech json"])
def test_the_worker_refuses_before_writing(tmp_path, capsys, case):
    if not LYP_PATH.is_file():
        pytest.skip("interposer LYP not found: %s" % LYP_PATH)
    kwargs = {}
    if case == "version":
        document = stage(tmp_path, lambda d: d.update(version="2.0"))
    elif case == "via table":
        document = stage(tmp_path, _two_cuts(pitch=TV2["size"] + TV2["sep"] + 0.01))
    else:
        document = stage(tmp_path)
        kwargs["tech_json_path"] = str(tmp_path / "absent.json")
    ok, out = _convert(tmp_path, document, **kwargs)
    err = capsys.readouterr().err
    assert ok is False
    assert "Routed geometry refused" in err and "no layout" in err
    assert not out.parent.exists() or not any(out.parent.iterdir())


def test_the_worker_takes_a_hyp_or_the_routed_geometry(monkeypatch, capsys):
    for argv in (["hyp_to_gds.py"], ["hyp_to_gds.py", "x.hyp", "--routed-geometry", "d.json"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            _h.main()
        assert exc.value.code == 2
        assert "--routed-geometry" in capsys.readouterr().err


def test_a_valid_tech_json_the_document_disagrees_with_is_refused(tmp_path, capsys):
    """The generator sizes vias from the --tech-json load_via_table checked:
    a readable table whose TopVia2 cut is not the document's refuses the
    run before anything is drawn, and the same table with the document's
    cut lets it through."""
    if not LYP_PATH.is_file():
        pytest.skip("interposer LYP not found: %s" % LYP_PATH)
    document = stage(tmp_path)
    for name, size, ok_expected in (("other", 0.8, False), ("same", 0.9, True)):
        tech = tmp_path / ("%s.json" % name)
        tech.write_text(json.dumps({"rules": dict(FULL_RULES, TV2_a=size)}))
        out = tmp_path / name / "interposer.gds"
        out.parent.mkdir()
        ok = _h.convert_hyp_to_gds("", str(out), str(LYP_PATH),
                                   routed_geometry_path=str(document),
                                   tech_json_path=str(tech))
        err = capsys.readouterr().err
        assert ok is ok_expected, err
        if not ok_expected:
            assert "TopVia2 cut is 0.9, the engine's TV2 table gives 0.8" in err
            assert not out.exists()
