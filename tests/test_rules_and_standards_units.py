"""Unit tests (no pcbnew) for MCP_ISSUES C2 / §2.9 helpers and the board-edit dispatch.

* ``utils.kicad_dru`` rule parsing/validation/editing, including the value
  forms KiCad 10.0.8 silently rejects (each verified with kicad-cli: one bad
  rule makes KiCad drop the WHOLE rules file);
* ``utils.standards_profile`` (the YAML-subset fallback for KiCad's Python,
  which has no PyYAML) and net pattern matching;
* ``utils.poly2d`` geometry;
* the interface's handling of ``_skipAutoSave`` / ``_autoSaveStatus``.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils import poly2d as g  # noqa: E402
from utils.kicad_dru import (  # noqa: E402
    DruSyntaxError,
    append_rule_text,
    parse_rules_file,
    parse_sexpr,
    remove_rule_spans,
)
from utils.standards_profile import (  # noqa: E402
    ProfileError,
    layer_name,
    net_matches,
    normalize_profile,
    parse_yaml_subset,
)

pytestmark = pytest.mark.unit

USER_DRU = """(version 1)

# RP2350 core-regulator switching node
# second comment line
(rule "RPi core VREG_LX clearance"
\t(condition "A.NetName == '/MCU/VREG_LX' || B.NetName == '/MCU/VREG_LX'")
\t(constraint clearance (min 0.12)))

(rule "keep"
\t(constraint track_width (min 0.2mm)))
"""


# --------------------------------------------------------------------------- kicad_dru


def test_parse_sexpr_reports_line_and_column():
    with pytest.raises(DruSyntaxError) as exc:
        parse_sexpr('(version 1)\n(rule "a"\n  (constraint clearance (min 0.2mm))\n')
    assert exc.value.line == 2
    with pytest.raises(DruSyntaxError, match="unterminated string"):
        parse_sexpr('(rule "a)')
    with pytest.raises(DruSyntaxError, match="unexpected '\\)'"):
        parse_sexpr("(a))")


def test_unitless_length_is_an_error_because_kicad_drops_the_file():
    parsed = parse_rules_file(USER_DRU)
    assert [r["name"] for r in parsed["rules"]] == ["RPi core VREG_LX clearance", "keep"]
    assert len(parsed["errors"]) == 1
    assert "no unit" in parsed["errors"][0] and "WHOLE" in parsed["errors"][0]


@pytest.mark.parametrize(
    "rule, problem",
    [
        (
            '(rule "s" (condition "A.NetName == \'GND\'") (constraint min_resolved_spokes (min 2)))',
            "bare count",
        ),
        ('(rule "a" (constraint track_angle (min 45deg)))', "plain degrees"),
        ('(rule "t" (constraint clearence (min 0.1mm)))', "not a KiCad 10 constraint"),
        ('(rule "c" (condition "A.NetName = \'GND\'") (constraint clearance (min 0.1mm)))', "'='"),
        (
            '(rule "q" (condition "A.NetName == \'GND") (constraint clearance (min 0.1mm)))',
            "quotes",
        ),
        ('(rule "n" (condition "A.NetName == \'GND\'"))', "no (constraint"),
        ('(rule "w" (when "x") (constraint clearance (min 0.1mm)))', "unknown clause"),
        ('(rule "v" (severity loud) (constraint clearance (min 0.1mm)))', "severity"),
    ],
)
def test_rejected_forms(rule, problem):
    errors = parse_rules_file(rule)["errors"]
    assert errors and any(problem in e for e in errors), errors


@pytest.mark.parametrize(
    "rule",
    [
        '(rule "s" (condition "A.NetName == \'GND\'") (constraint min_resolved_spokes 2))',
        '(rule "a" (constraint track_angle (min 45)))',
        '(rule "v" (constraint via_count (max 20)))',
        '(rule "z" (condition "A.NetName == \'GND\'") (constraint zone_connection solid))',
        '(rule "d" (layer outer) (severity warning) (constraint disallow via))',
        '(rule "x" (condition "A.Type == \'Pad\'") (constraint assertion "A.Type == \'Via\'"))',
    ],
)
def test_accepted_forms(rule):
    assert parse_rules_file(rule)["errors"] == []


def test_remove_rule_takes_attached_comments_and_keeps_the_rest():
    parsed = parse_rules_file(USER_DRU)
    first = parsed["rules"][0]
    out = remove_rule_spans(USER_DRU, [(first["start"], first["end"])])
    assert "RP2350" not in out and "VREG_LX" not in out
    assert out.startswith("(version 1)\n")
    again = parse_rules_file(out)
    assert [r["name"] for r in again["rules"]] == ["keep"]
    assert again["errors"] == []


def test_append_rule_text_creates_version_header():
    out = append_rule_text("", '(rule "a" (constraint clearance (min 0.2mm)))')
    assert out.startswith("(version 1)\n\n(rule")
    assert [r["name"] for r in parse_rules_file(out)["rules"]] == ["a"]


def test_add_drc_rule_refuses_when_existing_file_is_rejected(tmp_path):
    from commands.drc_rules import DrcRuleCommands

    (tmp_path / "p.kicad_pro").write_text("{}")
    (tmp_path / "p.kicad_dru").write_text(USER_DRU)
    board = MagicMock()
    board.GetFileName.return_value = str(tmp_path / "p.kicad_pcb")
    cmd = DrcRuleCommands(board)
    res = cmd.add_drc_rule({"ruleText": '(rule "new" (constraint track_width (min 0.3mm)))'})
    assert res["success"] is False and "ignores the whole file" in res["message"]
    # Replacing the broken rule with a fixed one is allowed (syntax-only here).
    fixed = (
        '(rule "RPi core VREG_LX clearance" (condition "A.NetName == \'/MCU/VREG_LX\'") '
        "(constraint clearance (min 0.12mm)))"
    )
    res = cmd.add_drc_rule({"ruleText": fixed, "replace": True, "measure": False})
    assert res["success"] is True, res
    text = (tmp_path / "p.kicad_dru").read_text()
    assert "(min 0.12mm)" in text and "(min 0.12))" not in text
    listed = cmd.list_drc_rules({})
    assert listed["fileRejected"] is False
    assert [r["name"] for r in listed["rules"]] == ["keep", "RPi core VREG_LX clearance"]
    removed = cmd.remove_drc_rule({"names": ["keep"]})
    assert removed["success"] and removed["remaining"] == 1
    assert cmd.remove_drc_rule({"name": "nope"})["success"] is False


def test_add_drc_rule_duplicate_needs_replace(tmp_path):
    from commands.drc_rules import DrcRuleCommands

    (tmp_path / "p.kicad_pro").write_text("{}")
    board = MagicMock()
    board.GetFileName.return_value = str(tmp_path / "p.kicad_pcb")
    cmd = DrcRuleCommands(board)
    rule = '(rule "a" (constraint clearance (min 0.2mm)))'
    assert cmd.add_drc_rule({"ruleText": rule, "measure": False})["success"]
    dup = cmd.add_drc_rule({"ruleText": rule, "measure": False})
    assert dup["success"] is False and "already exist" in dup["message"]
    dry = cmd.add_drc_rule(
        {
            "ruleText": '(rule "b" (constraint clearance (min 0.3mm)))',
            "measure": False,
            "dryRun": True,
        }
    )
    assert dry["success"] and dry["written"] is False
    assert '"b"' not in (tmp_path / "p.kicad_dru").read_text()
    bad = cmd.add_drc_rule({"ruleText": '(rule "c" (constraint clearance (min 0.3mm))'})
    assert bad["success"] is False and bad["syntaxOk"] is False


# --------------------------------------------------------------------------- profile

PROFILE = """fab: { name: jlcpcb, layers: 4, min_track: 0.1, min_space: 0.1, min_drill: 0.3, min_annular: 0.1,
       silk_min_height: 0.8, silk_min_width: 0.15, via_default: [0.6, 0.3] }
planes:
  In1: { net: GND, split: false }                     # one solid reference, never split
  In2: { regions: { "+3V3": digital, "+5V": power, GND: analog } }
regions:
  analog:  { polygon: [[66,62],[92,62],[92,56],[100,56],[100,100],[66,100]],
             allowed_nets: ["/Audio/*", "+3V3A", "CODEC_*", GND, "+3V3"],
             fence: { pitch: 1.5 }, stitch: 2.5 }
  power:   { polygon: [[0,72],[66,72],[66,100],[0,100]] }
netclasses:
  FAST:   { nets: ["I2S_*", CODEC_MCLK], no_plane_split_crossing: true, prefer: F.Cu }
  PWR_HI: { width: 1.0, nets: ["/Power/+12V", "+5V_*"] }
nets:
  "+12V": { forbid_regions: [analog] }
checks:
  every_gnd_smd_pad_has_via_within: 1.3
  exposed_pads_have_via_array: true
  decoupling_within_mm:
    distance: 3
    ic_prefixes:
      - U
  reference_layouts: [{ source: rpi_rp2350b_minimal, anchor: U7 }]
"""


def test_yaml_subset_parses_the_issue_example():
    data = parse_yaml_subset(PROFILE)
    assert data["fab"]["min_drill"] == 0.3 and data["fab"]["via_default"] == [0.6, 0.3]
    assert data["planes"]["In2"]["regions"]["+3V3"] == "digital"
    assert data["regions"]["analog"]["polygon"][2] == [92, 56]
    assert data["netclasses"]["FAST"]["no_plane_split_crossing"] is True
    assert data["checks"]["decoupling_within_mm"] == {"distance": 3, "ic_prefixes": ["U"]}
    assert data["checks"]["reference_layouts"] == [
        {"source": "rpi_rp2350b_minimal", "anchor": "U7"}
    ]
    try:
        import yaml  # type: ignore
    except ImportError:
        return
    assert data == yaml.safe_load(PROFILE)


def test_yaml_subset_errors_name_the_line():
    with pytest.raises(ProfileError, match=":2"):
        parse_yaml_subset("a: 1\n  b: 2\n")
    with pytest.raises(ProfileError, match="unbalanced"):
        parse_yaml_subset("a: [1, 2\n")


def test_normalize_profile():
    prof = normalize_profile(parse_yaml_subset(PROFILE))
    assert set(prof["planes"]) == {"In1.Cu", "In2.Cu"}
    assert prof["planes"]["In2.Cu"]["split"] is True
    assert prof["netclasses"]["FAST"]["prefer"] == "F.Cu"
    assert prof["regions"]["power"]["allowed_nets"] is None
    with pytest.raises(ProfileError, match="unknown region"):
        normalize_profile({"nets": {"+12V": {"forbid_regions": ["nowhere"]}}})
    with pytest.raises(ProfileError, match="polygon"):
        normalize_profile({"regions": {"r": {"allowed_nets": ["GND"]}}})


def test_net_matching_handles_hierarchy():
    assert net_matches("/Power/+5V", "+5V")
    assert net_matches("/Audio/LINE_L", "/Audio/*")
    assert not net_matches("/Power/+5V", "/Audio/*")
    assert net_matches("I2S_BCLK", "I2S_*")
    assert not net_matches("+5V", "/Power/+5V")
    assert layer_name("In2") == "In2.Cu" and layer_name("B") == "B.Cu"


# --------------------------------------------------------------------------- poly2d


def test_polygon_predicates():
    sq = g.rect_polygon(0, 0, 10, 10)
    assert g.point_in_polygon((5, 5), sq) and g.point_in_polygon((10, 5), sq)
    assert not g.point_strictly_inside((10, 5), sq)
    # Along the border does not enter; crossing does.
    assert not g.segment_enters_polygon((0, -1), (0, 11), sq)
    assert g.segment_enters_polygon((-5, 5), (15, 5), sq)
    assert g.segment_touches_polygon((10, 5), (20, 5), sq)
    other = g.rect_polygon(10, 0, 20, 10)  # shares an edge
    assert not g.polygons_overlap_strictly(sq, other)
    assert g.polygons_overlap_strictly(sq, g.rect_polygon(9, 9, 12, 12))
    l_shape = g.parse_polygon([[66, 62], [92, 62], [92, 56], [100, 56], [100, 100], [66, 100]])
    assert g.point_in_polygon((95, 58), l_shape) and not g.point_in_polygon((70, 58), l_shape)
    assert g.polygon_inside(g.rect_polygon(1, 1, 2, 2), sq)
    assert not g.polygon_inside(g.rect_polygon(9, 9, 11, 11), sq)
    assert g.area(l_shape) == pytest.approx(34 * 38 + 8 * 6)


def test_rotate_matches_kicad_screen_convention():
    x, y = g.rotate((1, 0), 90)
    assert (round(x, 9), round(y, 9)) == (0, -1)


# --------------------------------------------------------------------------- dispatch


@pytest.fixture()
def iface():
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        yield KiCADInterface()


def test_dispatcher_skips_auto_save_for_dry_runs(iface):
    iface.command_routes["add_copper"] = lambda p: {
        "success": True,
        "dryRun": True,
        "_skipAutoSave": True,
    }
    iface._auto_save_board = MagicMock(return_value={"saved": True})
    res = iface.handle_command("add_copper", {"dryRun": True})
    assert res["success"] and "_skipAutoSave" not in res
    iface._auto_save_board.assert_not_called()


def test_dispatcher_uses_the_handlers_own_save(iface):
    status = {"saved": False, "warning": "changed externally", "memChangesUnsaved": True}
    iface.command_routes["unroute"] = lambda p: {"success": True, "_autoSaveStatus": status}
    iface._auto_save_board = MagicMock(return_value={"saved": True})
    res = iface.handle_command("unroute", {"nets": ["X"], "drcDelta": True})
    iface._auto_save_board.assert_not_called()
    assert "_autoSaveStatus" not in res
    assert res["autoSave"] == status and "changed externally" in res["warnings"]
    assert iface._last_auto_save_status == status


def test_dispatcher_auto_saves_ordinary_mutations(iface):
    iface.command_routes["split_plane"] = lambda p: {"success": True}
    iface._auto_save_board = MagicMock(return_value={"saved": True})
    iface.handle_command("split_plane", {})
    iface._auto_save_board.assert_called_once()
