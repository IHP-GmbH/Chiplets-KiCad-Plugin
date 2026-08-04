# SPDX-License-Identifier: GPL-3.0-or-later
"""Dependency discovery and settings persistence."""
import json

import pytest

from resizer_passive_elements import paths


class FakeBoard:
    def __init__(self, filename=""):
        self._filename = filename

    def GetFileName(self):
        return self._filename

    def GetProject(self):
        return None


def _make_checkout(root):
    """A directory shaped like the interposer PDK, with the two files used."""
    tech = root.joinpath(*paths._TECH_JSON_RELATIVE)
    gen = root.joinpath(*paths._GEN_SCRIPT_RELATIVE)
    for path in (tech, gen):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return tech, gen


def test_interposer_pdk_root_is_tried_before_the_alias(tmp_path, monkeypatch):
    # INTERPOSER_PDK_ROOT is the ecosystem-wide variable (chiplet_export and
    # hyp_to_gds both use it), so one setting must configure every tool.
    preferred, alias = tmp_path / "preferred", tmp_path / "alias"
    tech, _gen = _make_checkout(preferred)
    _make_checkout(alias)
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(preferred))
    monkeypatch.setenv("INTM4TM2_ROOT", str(alias))

    assert paths.discover_tech_json_path() == str(tech)


def test_the_legacy_alias_still_resolves(tmp_path, monkeypatch):
    tech, gen = _make_checkout(tmp_path / "only-alias")
    monkeypatch.delenv("INTERPOSER_PDK_ROOT", raising=False)
    monkeypatch.setenv("INTM4TM2_ROOT", str(tmp_path / "only-alias"))

    assert paths.discover_tech_json_path() == str(tech)
    assert paths.discover_footprint_gen_path() == str(gen)


def test_a_bogus_root_falls_through_instead_of_winning(tmp_path, monkeypatch):
    bogus = tmp_path / "does-not-exist"
    monkeypatch.setenv("INTERPOSER_PDK_ROOT", str(bogus))
    monkeypatch.delenv("INTM4TM2_ROOT", raising=False)

    # A set-but-invalid variable must not shortcut discovery: the result is
    # either a later leg's real file or nothing, never a path under the bogus
    # root. Same rule as _discover_path_var in the sibling plugin's worker.
    resolved = paths.discover_tech_json_path(FakeBoard())

    assert not resolved.startswith(str(bogus))
    assert resolved == "" or paths.Path(resolved).is_file()


def test_the_ecosystem_checkout_name_is_a_sibling_candidate():
    # The checkout is called "interposer" in this ecosystem; leaving it out of
    # the list is what made the sibling leg never fire.
    assert "interposer" in paths._SIBLING_NAMES
    assert paths._SIBLING_NAMES[0] == "interposer"


def test_sibling_roots_walk_every_ancestor():
    roots = list(paths._sibling_roots())
    plugin_dir = paths.Path(paths.__file__).resolve().parent

    # The plugin lives at <repo>/plugins/<name>, so the repo's own parent has
    # to be reachable: that is where a sibling checkout actually sits.
    assert plugin_dir.parent.parent.parent / "interposer" in roots
    assert len(roots) > 3 * len(paths._SIBLING_NAMES)


def test_resolve_from_root_reports_each_file_separately(tmp_path):
    tech, gen = _make_checkout(tmp_path / "root")
    assert paths.resolve_from_root(str(tmp_path / "root")) == (str(tech),
                                                               str(gen))
    assert paths.resolve_from_root(str(tmp_path / "empty")) == ("", "")
    assert paths.resolve_from_root("") == ("", "")


def test_output_dir_defaults_next_to_the_board(tmp_path):
    board_file = tmp_path / "design" / "board.kicad_pcb"
    board_file.parent.mkdir()
    board_file.write_text("")

    out = paths.discover_output_pretty_dir(FakeBoard(str(board_file)))

    # Never inside the read-only PDK checkout.
    assert out == str(board_file.parent / paths._DEFAULT_OUTPUT_DIRNAME)


def test_path_overrides_round_trip_next_to_the_board(tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("")
    board = FakeBoard(str(board_file))

    assert paths.save_path_overrides(board, "/root", "/tech.json", "/gen.py",
                                     "/out.pretty") is True
    assert paths.load_path_overrides(board) == ("/root", "/tech.json",
                                                "/gen.py", "/out.pretty")


def test_saved_root_feeds_discovery(tmp_path, monkeypatch):
    # Environment first, so a machine that exports one of the variables (the
    # ADK-Tools image does) would otherwise win this lookup.
    for var in paths.REPO_ROOT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    tech, _gen = _make_checkout(tmp_path / "root")
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("")
    board = FakeBoard(str(board_file))
    paths.save_path_overrides(board, str(tmp_path / "root"), "", "", "")

    assert paths.discover_tech_json_path(board) == str(tech)


def test_saving_without_a_board_file_says_so(tmp_path):
    logged = []

    assert paths.save_path_overrides(FakeBoard(""), "/root", "", "", "",
                                     on_log=logged.append) is False
    assert logged, "a persistence no-op has to be visible, not silent"
    assert paths.load_path_overrides(FakeBoard("")) == ("", "", "", "")


def test_a_corrupt_settings_file_is_ignored(tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("")
    (tmp_path / paths._SETTINGS_FILENAME).write_text("{not json")

    assert paths.load_path_overrides(FakeBoard(str(board_file))) == ("", "",
                                                                    "", "")


def test_settings_are_written_as_readable_json(tmp_path):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("")
    paths.save_path_overrides(FakeBoard(str(board_file)), "/root", "", "", "")

    data = json.loads((tmp_path / paths._SETTINGS_FILENAME).read_text())

    assert data[paths.ROOT_DIR_TEXT_VAR] == "/root"


@pytest.mark.parametrize("board", [None, FakeBoard("")])
def test_discovery_never_raises_without_a_usable_board(board):
    assert isinstance(paths.discover_tech_json_path(board), str)
    assert isinstance(paths.discover_footprint_gen_path(board), str)
