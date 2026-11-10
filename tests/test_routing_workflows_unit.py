"""Unit tests for the job framework and the autoroute orchestration helpers.

No pcbnew needed: the job framework is plain threads/subprocesses, and the DSN /
SES rewriting, Freerouting log parsing, DRC summarising and import decision are
pure text/dict functions. The pcbnew-backed parts are in
test_routing_workflows_real.py.
"""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from commands import autoroute_pipeline as ap  # noqa: E402
from commands.job_commands import JobCommands  # noqa: E402
from utils import jobs as jobs_mod  # noqa: E402
from utils.jobs import JOBS, JobManager, run_process  # noqa: E402

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- jobs


def _wait_state(mgr, job_id, states, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        job = mgr.get(job_id)
        if job.state in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {mgr.get(job_id).state}")


def test_job_runs_in_background_and_reports_result():
    mgr = JobManager()
    gate = threading.Event()

    def work(ctx):
        ctx.progress(0.25, "started")
        ctx.log("line 1")
        gate.wait(5)
        ctx.progress(0.75, "almost")
        return {"success": True, "value": 42}

    job = mgr.start("demo", work, params={"a": 1})
    snap = _wait_state(mgr, job.id, ("running",)).snapshot()
    assert snap["state"] == "running"
    assert snap["kind"] == "demo"
    gate.set()
    job = _wait_state(mgr, job.id, ("succeeded",))
    snap = job.snapshot(log_lines=5)
    assert snap["result"] == {"success": True, "value": 42}
    assert snap["progress"] == 1.0
    assert snap["logTail"] == ["line 1"]
    assert snap["params"] == {"a": 1}
    assert "applyStep" not in snap


def test_job_failure_and_exception_states_and_cleanup():
    mgr = JobManager()
    cleaned = []
    j1 = mgr.start(
        "demo",
        lambda ctx: {"success": False, "message": "nope"},
        cleanup_fn=lambda job: cleaned.append(job.id),
    )
    j2 = mgr.start("demo", lambda ctx: 1 / 0)
    _wait_state(mgr, j1.id, ("failed",))
    _wait_state(mgr, j2.id, ("failed",))
    assert mgr.get(j1.id).error == "nope"
    assert "ZeroDivisionError" in mgr.get(j2.id).error
    assert cleaned == [j1.id]  # failed jobs free their files at once


def test_stalled_flag_follows_progress_not_output():
    mgr = JobManager()
    stop = threading.Event()

    def work(ctx):
        while not stop.is_set():
            ctx.log("noise")  # output alone is not progress
            time.sleep(0.02)
        return {"success": True}

    job = mgr.start("demo", work, stall_timeout=0.2)
    time.sleep(0.5)
    assert job.snapshot()["stalled"] is True
    stop.set()
    _wait_state(mgr, job.id, ("succeeded",))
    assert job.snapshot()["stalled"] is False


def test_run_process_streams_output_and_kills_on_stall(tmp_path):
    mgr = JobManager()
    script = tmp_path / "chatty.py"
    script.write_text(
        "import time, sys\n"
        "for i in range(200):\n"
        "    print('tick', i, flush=True)\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    seen = []

    def work(ctx):
        res = run_process(
            ctx, [sys.executable, str(script)], stall_timeout=0.6, on_line=seen.append
        )
        return {"success": True, **res}

    job = mgr.start("demo", work)
    job = _wait_state(mgr, job.id, ("succeeded",), timeout=20)
    assert job.result["reason"] == "stalled"
    assert job.result["elapsed"] < 5
    assert seen and seen[0] == "tick 0"
    assert job.result["tail"][-1].startswith("tick")


def test_cancel_kills_child_process(tmp_path):
    mgr = JobManager()
    script = tmp_path / "sleepy.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    def work(ctx):
        return {"success": True, **run_process(ctx, [sys.executable, str(script)])}

    job = mgr.start("demo", work)
    _wait_state(mgr, job.id, ("running",))
    time.sleep(0.3)
    t0 = time.time()
    mgr.cancel(job.id)
    job = _wait_state(mgr, job.id, ("cancelled",), timeout=15)
    assert time.time() - t0 < 10
    assert job.result["reason"] == "cancelled"


def test_apply_step_runs_once_only_after_success():
    mgr = JobManager()
    calls = []

    def apply_fn(job, params):
        calls.append(params)
        return {"success": True, "message": "applied", "_boardFileWritten": "disk"}

    gate = threading.Event()
    job = mgr.start("demo", lambda ctx: gate.wait(5) and {"success": True}, apply_fn=apply_fn)
    early = mgr.apply(job.id, {})
    assert early["success"] is False and "running" in early["message"]
    gate.set()
    _wait_state(mgr, job.id, ("succeeded",))
    first = mgr.apply(job.id, {"importAnyway": True})
    assert first["success"] is True
    assert first["_boardFileWritten"] == "disk"  # passed through to the dispatcher
    assert calls == [{"importAnyway": True}]
    second = mgr.apply(job.id, {})
    assert second["success"] is False and "already applied" in second["message"]
    snap = job.snapshot()
    assert snap["applyStep"]["applied"] is True
    assert "_boardFileWritten" not in snap["applyResult"]


def test_job_commands_tools(monkeypatch):
    mgr = JobManager()
    monkeypatch.setattr("commands.job_commands.JOBS", mgr)
    cmds = JobCommands()
    gate = threading.Event()
    job = mgr.start("demo", lambda ctx: gate.wait(5) and {"success": True, "x": 1})
    assert cmds.job_status({})["success"] is False
    assert cmds.job_status({"jobId": "nope"})["success"] is False
    st = cmds.job_status({"jobId": job.id, "logLines": 0})
    assert st["success"] and st["state"] in ("queued", "running")
    listed = cmds.job_list({"kind": "demo"})
    assert listed["count"] == 1 and "result" not in listed["jobs"][0]
    assert cmds.job_list({"states": ["succeeded"]})["count"] == 0
    gate.set()
    _wait_state(mgr, job.id, ("succeeded",))
    assert cmds.job_status({"jobId": job.id})["result"]["x"] == 1
    assert "already finished" in cmds.job_cancel({"jobId": job.id})["message"]
    assert "no apply step" in cmds.job_apply({"jobId": job.id})["message"]


def test_finished_jobs_are_pruned(monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_FINISHED_JOBS", 3)
    mgr = JobManager()
    cleaned = []
    ids = []
    for _ in range(6):
        job = mgr.start(
            "demo", lambda ctx: {"success": True}, cleanup_fn=lambda j: cleaned.append(j.id)
        )
        _wait_state(mgr, job.id, ("succeeded",))
        ids.append(job.id)
    mgr._prune()
    assert len(mgr.list()) == 3
    assert [j.id for j in mgr.list()] == ids[-3:]
    assert cleaned == ids[:3]


# ----------------------------------------------------------------- DSN rewrites

DSN = """(pcb test.dsn
  (parser
    (string_quote ")
  )
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu
      (type signal)
    )
    (layer B.Cu
      (type signal)
    )
    (boundary
      (path pcb 0  0 0  30000 0  30000 -20000  0 -20000  0 0)
    )
    (keepout "" (polygon F.Cu 0  5000 -5000  10000 -5000  10000 -8000  5000 -8000  5000 -5000))
    (keepout "trk" (polygon F.Cu 0  15000 -5000  20000 -5000  20000 -8000  15000 -8000))
    (via_keepout "" (polygon B.Cu 0  1000 -1000  2000 -1000  2000 -2000  1000 -2000))
    (via "Via[0-1]_600:300_um")
    (rule
      (width 250)
      (clearance 200)
    )
  )
  (network
    (net A
      (pins R1-1 R2-1)
    )
    (net "unconnected-(R3-Pad1)"
      (pins R3-1)
    )
    (net GND
      (pins R1-2 R2-2 R3-2)
    )
    (class kicad_default A GND "unconnected-(R3-Pad1)"
      (circuit
        (use_via "Via[0-1]_600:300_um")
      )
    )
  )
  (wiring
    (wire (path F.Cu 250  1000 -1000  2000 -1000)(net A)(type route))
    (wire (path F.Cu 250  3000 -1000  4000 -1000)(net GND)(type route))
    (via "Via[0-1]_600:300_um"  4000 -1000 (net GND)(type fix))
  )
)
"""

RULE_AREAS = [
    # pour-only rule area matching the first keepout
    {
        "name": "",
        "copperLayers": ["F.Cu"],
        "noTracks": False,
        "noVias": False,
        "bbox": [5, 5, 10, 8],
    },
    # track keepout matching the second one
    {
        "name": "trk",
        "copperLayers": ["F.Cu"],
        "noTracks": True,
        "noVias": True,
        "bbox": [15, 5, 20, 8],
    },
]


def test_strip_pour_only_keepouts_keeps_real_ones():
    out, rep = ap.strip_rule_area_keepouts(DSN, RULE_AREAS, "pour_only")
    assert rep["strippedCount"] == 1
    assert "5000 -5000  10000" not in out
    assert '(keepout "trk"' in out
    assert "(via_keepout" in out  # unmatched keepouts are never touched
    assert out.count("(") - out.count(")") == 0


def test_strip_all_and_none():
    out, rep = ap.strip_rule_area_keepouts(DSN, RULE_AREAS, "all")
    assert rep["strippedCount"] == 2 and '(keepout "trk"' not in out
    out, rep = ap.strip_rule_area_keepouts(DSN, RULE_AREAS, "none")
    assert out == DSN and rep["stripped"] == []


def test_strip_requires_layer_and_bbox_match():
    other_layer = [dict(RULE_AREAS[0], copperLayers=["B.Cu"])]
    out, rep = ap.strip_rule_area_keepouts(DSN, other_layer, "pour_only")
    assert rep["stripped"] == [] and out == DSN
    shifted = [dict(RULE_AREAS[0], bbox=[5.5, 5, 10, 8])]
    assert ap.strip_rule_area_keepouts(DSN, shifted, "pour_only")[1]["stripped"] == []


def test_lock_existing_wiring_only_touches_wiring():
    out, n = ap.lock_existing_wiring(DSN)
    assert n == 2
    wiring = out[out.index("(wiring") :]
    assert "(type route)" not in wiring
    assert wiring.count("(type fix)") == 3


def test_restrict_to_nets():
    out, rep = ap.restrict_to_nets(DSN, ["A", "NOPE"])
    assert rep["routedNets"] == ["A"]
    assert rep["unknownNets"] == ["NOPE"]
    assert rep["excludedNets"] == 2
    assert "(pins R1-1 R2-1)" in out
    assert "(pins R1-2 R2-2 R3-2)" not in out and "(pins R3-1)" not in out
    wiring = out[out.index("(wiring") :]
    assert "(net A)(type route)" in wiring  # routed net stays movable
    assert "(net GND)(type fix)" in wiring and "(net GND)(type route)" not in wiring
    assert rep["otherNetWiringFixed"] == 1


def test_restrict_to_quoted_net():
    out, rep = ap.restrict_to_nets(DSN, ["unconnected-(R3-Pad1)"])
    assert rep["routedNets"] == ["unconnected-(R3-Pad1)"]
    assert "(pins R3-1)" in out


def test_add_region_keepouts():
    out, n = ap.add_region_keepouts(
        DSN, [{"polygon": [(1, 1), (2, 1), (2, 2)], "layers": ["F.Cu", "B.Cu"]}]
    )
    assert n == 2
    assert '(keepout "mcp_forbid_0" (polygon F.Cu 0  1000.0 -1000.0' in out
    struct = out[out.index("(structure") : out.index("(network")]
    assert "mcp_forbid_0" in struct
    assert out.count("(") == out.count(")")


SES = """(session test
  (base_design test)
  (placement
    (resolution um 10)
    (component R
      (place R1 10000 -10000 front 0)
    )
  )
  (routes
    (resolution um 10)
    (network_out
      (net A
        (wire
          (path F.Cu 2500
            10000 -10000
            50000 -10000
          )
        )
        (via "Via[0-1]_600:300_um" 50000 -10000
        )
      )
      (net GND
        (wire
          (path F.Cu 2500
            30000 -10000
            40000 -10000
          )
          (type protect)
        )
      )
    )
  )
)
"""


def test_ses_helpers():
    assert ap.ses_nets(SES) == ["A", "GND"]
    kept, dropped = ap.filter_ses_nets(SES, {"A"})
    assert dropped == ["GND"] and "(net GND" not in kept and "(net A" in kept
    copper = ap.ses_new_copper(SES)
    assert set(copper) == {"A"}  # GND's wire is protected (pre-existing)
    kinds = [c[0] for c in copper["A"]]
    assert kinds == ["wire", "via"]
    wire = copper["A"][0]
    assert wire[1] == "F.Cu"
    assert wire[2] == [(1.0, 1.0), (5.0, 1.0)]  # um/10 units -> mm, y flipped
    square = [(2, 0), (3, 0), (3, 2), (2, 2)]
    assert ap._copper_enters(copper["A"], square, None) is True
    assert ap._copper_enters(copper["A"], square, {"B.Cu"}) is False
    far = [(10, 10), (11, 10), (11, 11)]
    assert ap._copper_enters(copper["A"], far, None) is False


def test_normalize_region():
    assert ap.normalize_region({"x1": 0, "y1": 0, "x2": 1, "y2": 2}) == [
        (0, 0),
        (1, 0),
        (1, 2),
        (0, 2),
    ]
    poly = ap.normalize_region({"polygon": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]})
    assert poly == [(0, 0), (1, 0), (1, 1)]
    with pytest.raises(ValueError):
        ap.normalize_region([[0, 0], [1, 1]])
    with pytest.raises(ValueError):
        ap.normalize_region({"x": 1})


# ------------------------------------------------------------ Freerouting log

LOG = [
    "2026-11-10 00:06:21.587 INFO   Freerouting v2.5.0 (build-date: 2026-10-05)",
    "2026-11-10 00:06:25.869 WARN   Design Warning: Board has 212 pre-existing clearance violation(s) in the loaded design (before routing):",
    "  - 212 unfixable violation(s) (cannot be resolved by Freerouting):",
    "2026-11-10 00:06:37.760 INFO   [B6] Fanout stage started on board 'x' with 260 of 660 SMD pins needing fanout",
    "2026-11-10 00:06:43.230 INFO   [B6] Fanout pass #1 on board 'd6' completed in 4.54 seconds with 47 SMD pins fanouted, 5 not routed",
    "2026-11-10 00:07:01.754 INFO   [B6] Auto-routing stage started on board '01' with baseline score 913.95 for 34 unrouted items.",
    "2026-11-10 00:07:14.226 INFO   [B6] Auto-routing pass #1 on board '01' was completed in 11.55 seconds with score 916.26 (33 unrouted and 212 violations), using",
    "2026-11-10 00:07:26.041 INFO   [B6] Auto-routing pass #2 on board 'b8' was completed in 10.85 seconds with score 916.26 (33 unrouted and 212 violations), using",
]


class _Ctx:
    def __init__(self):
        self.events = []

    def progress(self, fraction=None, message=None):
        self.events.append((fraction, message))


def test_freerouting_progress_parser():
    ctx = _Ctx()
    fp = ap.FreeroutingProgress(ctx, max_passes=10, attempt=1, attempts=1)
    counts = []
    for line in LOG:
        before = len(ctx.events)
        fp.feed(line)
        counts.append(len(ctx.events) - before)
    # loading line, stage, fanout with pins, stage, first pass improved; the
    # second pass (no better score / unrouted) is NOT progress
    assert counts[-1] == 0
    assert counts[-2] == 1
    summ = fp.summary()
    assert summ["unroutedReportedByFreerouting"] == 33
    assert summ["violationsReportedByFreerouting"] == 212
    assert summ["passes"] == 2
    assert summ["preexistingViolations"] == 212
    assert summ["unfixableViolations"] == 212
    assert summ["bestScore"] == 916.26
    fractions = [f for f, _ in ctx.events if f is not None]
    assert fractions == sorted(fractions) and all(0 < f < 1 for f in fractions)


# --------------------------------------------------------------- evaluation


def _drc(unconnected_nets, violations):
    return {
        "unconnected_items": [
            {
                "items": [
                    {"description": f"Pad 1 [{n}] of R1"},
                    {"description": f"Pad 2 [{n}] of R2"},
                ]
            }
            for n in unconnected_nets
        ],
        "violations": [
            {
                "type": t,
                "severity": sev,
                "description": t,
                "items": [{"description": d, "pos": {"x": 1, "y": 2}}],
            }
            for t, sev, d in violations
        ],
    }


def test_summarize_and_delta():
    before = ap.summarize_drc(
        _drc(["A", "A", "B"], [("clearance", "error", "Track [A]"), ("silk", "warning", "x")])
    )
    after = ap.summarize_drc(
        _drc(["B"], [("clearance", "error", "Track [C]"), ("silk", "warning", "x")])
    )
    assert before["unconnected"] == 3
    assert before["unconnectedByNet"] == {"A": 2, "B": 1}
    assert before["errors"] == 1 and before["warnings"] == 1
    delta = ap.drc_delta(before, after)
    assert delta["newCount"] == 1 and delta["resolvedCount"] == 1
    assert delta["new"][0]["items"] == ["Track [C]"]
    assert delta["unconnectedBefore"] == 3 and delta["unconnectedAfter"] == 1


def test_import_decision():
    b = {"unconnected": 5, "errors": 2}
    assert ap.import_decision("always", None, None)[0] is True
    assert ap.import_decision("if_better", b, {"unconnected": 3, "errors": 2})[0] is True
    assert ap.import_decision("if_better", b, {"unconnected": 3, "errors": 3})[0] is False
    assert ap.import_decision("if_better", b, {"unconnected": 5, "errors": 1})[0] is True
    assert ap.import_decision("if_better", b, {"unconnected": 5, "errors": 2})[0] is False
    assert ap.import_decision("if_better", None, {"unconnected": 1, "errors": 0})[0] is False


def test_wants_orchestration():
    assert ap.wants_orchestration({}) is False
    assert ap.wants_orchestration({"attempts": 3, "maxPasses": 5, "keepArtifacts": True}) is False
    for key in ap.ORCHESTRATION_KEYS:
        assert ap.wants_orchestration({key: True}) is True
    assert ap.wants_orchestration({"async": False}) is True


def test_orchestrated_option_validation(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from commands.freerouting import FreeroutingCommands

    board_file = tmp_path / "b.kicad_pcb"
    board_file.write_text("(kicad_pcb)", encoding="utf-8")
    jar = tmp_path / "fr.jar"
    jar.write_bytes(b"")
    board = MagicMock()
    board.GetFileName.return_value = str(board_file)
    cmds = FreeroutingCommands(board)
    monkeypatch.setattr(cmds, "_board_net_names", lambda: ["GND", "A"])
    base = {"freeroutingJar": str(jar)}

    def call(**extra):
        return cmds._autoroute_orchestrated({**base, **extra}, str(board_file))

    assert "stripRuleAreas" in call(stripRuleAreas="some")["errorDetails"]
    assert "importMode" in call(importMode="maybe")["errorDetails"]
    assert "unknown nets" in call(nets=["GND", "NOPE"])["errorDetails"]
    assert "non-empty" in call(nets=[])["errorDetails"]
    assert "positive" in call(stallTimeout=-1)["errorDetails"]
    bad_region = call(forbidRegionsForNets=[{"nets": ["A"], "region": {"x1": 0}}])
    assert "forbidRegionsForNets[0]" in bad_region["errorDetails"]
    no_nets = call(forbidRegionsForNets=[{"region": {"x1": 0, "y1": 0, "x2": 1, "y2": 1}}])
    assert "nets is required" in no_nets["errorDetails"]
    missing = cmds._autoroute_orchestrated(
        {"freeroutingJar": str(tmp_path / "x.jar")}, str(board_file)
    )
    assert missing["message"] == "Freerouting JAR not found"
