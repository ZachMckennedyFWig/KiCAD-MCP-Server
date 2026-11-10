import { describe, it, expect } from "vitest";
import {
  computeCommandTimeout,
  DEFAULT_COMMAND_TIMEOUT_MS,
  LONG_COMMAND_TIMEOUT_MS,
  LONG_RUNNING_COMMANDS,
  AUTOROUTE_OVERHEAD_MS,
  JLCPCB_DOWNLOAD_TIMEOUT_MS,
  CHECK_COMMANDS,
  CHECK_COMMAND_TIMEOUT_MS,
  MEDIUM_COMMANDS,
  MEDIUM_COMMAND_TIMEOUT_MS,
  GUI_WAIT_OVERHEAD_MS,
  SCRIPT_OVERHEAD_MS,
  DEFAULT_TIMEOUT_GRACE_MS,
  computeTimeoutGrace,
  defaultTimeout,
  parseToolTimeoutOverrides,
} from "../src/command-timeout.js";

describe("computeCommandTimeout", () => {
  it("gives the JLCPCB catalog download an hour (#454)", () => {
    // Under the 30 s default the worker was restarted mid-download.
    expect(computeCommandTimeout("download_jlcpcb_database", {})).toBe(JLCPCB_DOWNLOAD_TIMEOUT_MS);
    expect(JLCPCB_DOWNLOAD_TIMEOUT_MS).toBeGreaterThan(LONG_COMMAND_TIMEOUT_MS);
  });

  it("gives ordinary commands the short default", () => {
    expect(computeCommandTimeout("get_board_info", {})).toBe(DEFAULT_COMMAND_TIMEOUT_MS);
    expect(computeCommandTimeout("place_component", { x: 1 })).toBe(DEFAULT_COMMAND_TIMEOUT_MS);
  });

  it("gives size-bound commands the blanket long allowance", () => {
    for (const command of LONG_RUNNING_COMMANDS) {
      expect(computeCommandTimeout(command, {})).toBe(LONG_COMMAND_TIMEOUT_MS);
    }
  });

  describe("autoroute (issue #251)", () => {
    it("is never left on the 30s default", () => {
      // The regression itself: autoroute was absent from the long-running list,
      // so Node abandoned the call while Freerouting was still routing and a
      // valid .ses had been written.
      const timeout = computeCommandTimeout("autoroute", {});
      expect(timeout).toBeGreaterThan(DEFAULT_COMMAND_TIMEOUT_MS);
      expect(timeout).toBeGreaterThanOrEqual(LONG_COMMAND_TIMEOUT_MS);
    });

    it("covers the caller's own per-attempt budget plus overhead", () => {
      // 900s per attempt is well past the blanket 600s ceiling.
      expect(computeCommandTimeout("autoroute", { timeout: 900 })).toBe(
        900 * 1000 + AUTOROUTE_OVERHEAD_MS,
      );
    });

    it("multiplies the budget by attempts for best-of-N", () => {
      // Python runs `attempts` sequential passes of `timeout` seconds each.
      expect(computeCommandTimeout("autoroute", { timeout: 300, attempts: 5 })).toBe(
        300 * 1000 * 5 + AUTOROUTE_OVERHEAD_MS,
      );
    });

    it("still waits at least the blanket allowance for small budgets", () => {
      expect(computeCommandTimeout("autoroute", { timeout: 10 })).toBe(LONG_COMMAND_TIMEOUT_MS);
    });

    it("falls back to the 300s default when timeout is absent or nonsense", () => {
      const expected = 300 * 1000 * 3 + AUTOROUTE_OVERHEAD_MS;
      for (const bad of [undefined, null, 0, -5, NaN, "abc", {}]) {
        expect(computeCommandTimeout("autoroute", { timeout: bad, attempts: 3 })).toBe(expected);
      }
    });

    it("accepts numeric strings, since MCP clients stringify params", () => {
      expect(computeCommandTimeout("autoroute", { timeout: "900" })).toBe(
        900 * 1000 + AUTOROUTE_OVERHEAD_MS,
      );
    });

    it("treats bad attempts values as a single attempt", () => {
      const single = computeCommandTimeout("autoroute", { timeout: 900, attempts: 1 });
      for (const bad of [undefined, 0, -3, NaN, "xyz"]) {
        expect(computeCommandTimeout("autoroute", { timeout: 900, attempts: bad })).toBe(single);
      }
    });

    it("floors fractional attempts rather than inflating the budget", () => {
      expect(computeCommandTimeout("autoroute", { timeout: 900, attempts: 2.9 })).toBe(
        computeCommandTimeout("autoroute", { timeout: 900, attempts: 2 }),
      );
    });

    it("tolerates a missing params object", () => {
      expect(computeCommandTimeout("autoroute")).toBe(LONG_COMMAND_TIMEOUT_MS);
    });
  });
});

describe("per-tool timeouts (MCP_ISSUES #4 / #5m)", () => {
  const env = {};

  it("gives batch schematic tools the long allowance, not 30 s", () => {
    for (const command of [
      "batch_add_components",
      "batch_add_and_connect",
      "batch_connect",
      "batch_add_no_connects",
      "batch_edit_schematic_components",
    ]) {
      expect(LONG_RUNNING_COMMANDS as readonly string[]).toContain(command);
      expect(computeCommandTimeout(command, {}, env)).toBe(LONG_COMMAND_TIMEOUT_MS);
    }
  });

  it("gives DRC/ERC and zone refill the check allowance", () => {
    for (const command of CHECK_COMMANDS) {
      expect(computeCommandTimeout(command, {}, env)).toBe(CHECK_COMMAND_TIMEOUT_MS);
    }
    expect(computeCommandTimeout("run_erc", {}, env)).toBeGreaterThan(DEFAULT_COMMAND_TIMEOUT_MS);
  });

  it("covers exports and sync", () => {
    for (const command of [
      "export_gerbers",
      "export_sch_pdf",
      "export_odb",
      "sync_schematic_to_board",
    ]) {
      expect(computeCommandTimeout(command, {}, env)).toBe(LONG_COMMAND_TIMEOUT_MS);
    }
  });

  it("gives project loading/saving a medium allowance", () => {
    for (const command of MEDIUM_COMMANDS) {
      expect(computeCommandTimeout(command, {}, env)).toBe(MEDIUM_COMMAND_TIMEOUT_MS);
    }
  });

  it("keeps the tiers disjoint", () => {
    const all = [...LONG_RUNNING_COMMANDS, ...CHECK_COMMANDS, ...MEDIUM_COMMANDS];
    expect(new Set(all).size).toBe(all.length);
  });

  it("scales route_bus with its parallel-search timeBudget", () => {
    // Default 60 s search plus a sequential fallback fits the long allowance.
    expect(computeCommandTimeout("route_bus", {}, env)).toBe(LONG_COMMAND_TIMEOUT_MS);
    expect(computeCommandTimeout("route_bus", { timeBudget: 600 }, env)).toBe(
      2 * 600 * 1000 + SCRIPT_OVERHEAD_MS,
    );
  });

  it("extends GUI waits by their own seconds parameters", () => {
    // kicad_run_drc waits 15 s + 60 s by default: more than the old 30 s cap.
    expect(computeCommandTimeout("kicad_run_drc", {}, env)).toBeGreaterThanOrEqual(
      (15 + 60) * 1000,
    );
    expect(computeCommandTimeout("kicad_run_drc", { timeout: 30, runTimeout: 600 }, env)).toBe(
      630 * 1000 + GUI_WAIT_OVERHEAD_MS,
    );
    expect(computeCommandTimeout("kicad_gui_wait_for", { timeout: 120 }, env)).toBe(
      120 * 1000 + GUI_WAIT_OVERHEAD_MS,
    );
    // Default 10 s wait plus overhead.
    expect(computeCommandTimeout("kicad_gui_wait_for", {}, env)).toBe(
      10 * 1000 + GUI_WAIT_OVERHEAD_MS,
    );
  });
});

describe("environment overrides", () => {
  it("KICAD_MCP_COMMAND_TIMEOUT_MS sets the default", () => {
    const env = { KICAD_MCP_COMMAND_TIMEOUT_MS: "90000" };
    expect(defaultTimeout(env)).toBe(90_000);
    expect(computeCommandTimeout("get_board_info", {}, env)).toBe(90_000);
    // Long tiers are unaffected by a smaller default ...
    expect(computeCommandTimeout("run_drc", {}, env)).toBe(CHECK_COMMAND_TIMEOUT_MS);
    // ... and the medium tier never drops below a larger default.
    expect(
      computeCommandTimeout("open_board", {}, { KICAD_MCP_COMMAND_TIMEOUT_MS: "200000" }),
    ).toBe(200_000);
  });

  it("ignores nonsense default values", () => {
    for (const bad of ["", "abc", "-5", "0"]) {
      expect(defaultTimeout({ KICAD_MCP_COMMAND_TIMEOUT_MS: bad })).toBe(
        DEFAULT_COMMAND_TIMEOUT_MS,
      );
    }
  });

  it("KICAD_MCP_TOOL_TIMEOUTS overrides individual tools, computed ones included", () => {
    const env = {
      KICAD_MCP_TOOL_TIMEOUTS: "batch_connect=1200000, autoroute=60000;bogus, x=abc",
    };
    expect(computeCommandTimeout("batch_connect", {}, env)).toBe(1_200_000);
    expect(computeCommandTimeout("autoroute", { timeout: 900 }, env)).toBe(60_000);
    expect(computeCommandTimeout("get_board_info", {}, env)).toBe(DEFAULT_COMMAND_TIMEOUT_MS);
    expect(parseToolTimeoutOverrides(env.KICAD_MCP_TOOL_TIMEOUTS)).toEqual({
      batch_connect: 1_200_000,
      autoroute: 60_000,
    });
  });

  it("KICAD_MCP_TIMEOUT_GRACE_MS sets the wedged-worker grace period", () => {
    expect(computeTimeoutGrace({})).toBe(DEFAULT_TIMEOUT_GRACE_MS);
    expect(computeTimeoutGrace({ KICAD_MCP_TIMEOUT_GRACE_MS: "1000" })).toBe(1000);
  });
});
