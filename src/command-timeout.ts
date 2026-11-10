/**
 * Per-command timeout policy for the Python worker bridge.
 *
 * Kept as a pure function so the policy is unit-testable without spawning a
 * worker — the same reason `tools/registry.ts` and `tools/tool-response.ts`
 * are separated out.
 *
 * What a timeout means (MCP_ISSUES #4 / #5m): when a command exceeds its
 * timeout, the bridge rejects the *caller* but does NOT kill the Python
 * worker. Killing it mid-command could interrupt a file write (a 30 s timeout
 * once left a `.kicad_sch` at 0 bytes). Instead the worker is left to finish;
 * its late response is discarded by request id (#373), and new calls fail
 * fast with a "still busy" error until it does. Only a worker that is still
 * busy `TIMEOUT_GRACE_MS` after the timeout is treated as wedged and restarted.
 *
 * Environment overrides (all values in milliseconds):
 *   KICAD_MCP_COMMAND_TIMEOUT_MS  default for commands with no specific entry
 *   KICAD_MCP_TOOL_TIMEOUTS       per-command overrides, "name=ms,name=ms"
 *                                 (wins over everything, including computed ones)
 *   KICAD_MCP_TIMEOUT_GRACE_MS    how long a timed-out command may keep running
 *                                 before the worker is considered wedged
 */

/** Fallback for commands that should answer promptly. */
export const DEFAULT_COMMAND_TIMEOUT_MS = 30_000;

/** Loading/saving a large project, launching tools: slower than a query. */
export const MEDIUM_COMMAND_TIMEOUT_MS = 120_000;

/** Blanket allowance for operations that are slow but not caller-tunable. */
export const LONG_COMMAND_TIMEOUT_MS = 600_000;

/** Full-board checks (DRC/ERC, zone refill) on big designs. */
export const CHECK_COMMAND_TIMEOUT_MS = 900_000;

/** After a timeout, how long the worker may keep running before it is restarted. */
export const DEFAULT_TIMEOUT_GRACE_MS = 600_000;

/**
 * Commands whose cost scales with the size of the thing being worked on rather
 * than with a parameter the caller can reason about up front.
 *
 * Batch schematic tools are here because each item re-reads and rewrites the
 * sheet and resolves library symbols: ~17 symbols on a 25-component sheet took
 * longer than 30 s and the timeout killed the write mid-way (MCP_ISSUES #4).
 *
 * `digikey_check_library_availability` is here for a different reason to the
 * rest: it issues one or two rate-limited HTTP requests per symbol and
 * self-throttles between them, so even its reduced default of 25 symbols cannot
 * finish inside `DEFAULT_COMMAND_TIMEOUT_MS` once network latency is included.
 * Since #382 a late response is discarded by request id rather than mis-delivered
 * to the next call, so the cost of timing out is now confined to this command —
 * but it is still the whole sweep's results, every one of them already paid for
 * against the caller's rate limit.
 */
export const LONG_RUNNING_COMMANDS = [
  // Exports (kicad-cli / plotters)
  "export_gerber",
  "export_gerbers",
  "export_gerber_single",
  "export_drill",
  "export_pdf",
  "export_svg",
  "export_3d",
  "export_3d_cli",
  "export_bom",
  "export_pos",
  "export_netlist",
  "export_dsn",
  "export_gencad",
  "export_ipc2581",
  "export_ipcd356",
  "export_odb",
  "export_pcb_dxf",
  "export_pcb_pdf",
  "export_pcb_svg",
  "export_sch_bom",
  "export_sch_dxf",
  "export_sch_hpgl",
  "export_sch_pdf",
  "export_sch_ps",
  "export_sch_python_bom",
  "export_sch_svg",
  "export_schematic_pdf",
  "export_schematic_svg",
  "generate_netlist",
  "get_board_2d_view",
  "get_schematic_view",
  "get_schematic_view_region",
  // Schematic <-> board sync and imports
  "sync_schematic_to_board",
  "create_board_from_schematic",
  "backannotate_footprints",
  "import_ses",
  "import_pcb",
  "import_eagle_project",
  "snapshot_project",
  "save_as",
  // Batch schematic edits and whole-sheet passes
  "batch_add_components",
  "batch_add_and_connect",
  "batch_connect",
  "batch_add_no_connects",
  "batch_edit_schematic_components",
  "batch_move_components",
  "batch_set_schematic_property_positions",
  "batch_list_symbol_pins",
  "hierarchical_place",
  "place_component_array",
  "annotate_schematic",
  "autoplace_schematic_fields",
  "update_symbol_from_library",
  "repair_flat_symbols",
  "replace_instance_lib_ids",
  "list_schematic_nets",
  "list_schematic_labels",
  "validate_schematic",
  "lint_schematic_cosmetic",
  "lint_offgrid",
  "suggest_schematic_declutter",
  "find_orphaned_wires",
  "find_wires_crossing_symbols",
  "add_gnd_stitching_vias",
  "enrich_datasheets",
  "project_status",
  // Network sweeps
  "digikey_check_library_availability",
] as const;

/** Full-design checks: DRC/ERC and zone refill scale with board/sheet size. */
export const CHECK_COMMANDS = [
  "run_drc",
  "run_erc",
  "get_drc_violations",
  "check_clearance",
  "check_courtyard_overlaps",
  "refill_zones",
  // Two kicad-cli DRC runs (rules before/after) on temporary copies.
  "add_drc_rule",
  "check_standards",
] as const;

/** Loading, saving and launching: usually quick, but a big project can exceed 30 s. */
export const MEDIUM_COMMANDS = [
  "open_project",
  "open_board",
  "reload_board",
  "discard_or_reload",
  "save_project",
  "save_board",
  "create_project",
  "close_project",
  "launch_kicad_ui",
  "install_gui_driver",
  "check_freerouting",
  "get_ratsnest",
  "estimate_airwire_lengths",
  "search_symbols",
  "search_footprints",
  "list_library_footprints",
  "list_library_symbols",
  "list_symbols_in_library",
  "validate_symbol_library",
  "find_duplicate_symbols",
  "search_jlcpcb_parts",
  "download_registry_part",
  // Whole-hierarchy rewrites, catalog scans and whole-board text diffs (S4, S5, I1)
  "apply_property_map",
  "jlc_bom_report",
  "jlc_find_alternative",
  "diff_snapshots",
  "restore_snapshot",
] as const;

/**
 * Extra wall-clock granted to `autoroute` on top of the routing budget the
 * caller asked for, covering DSN export, JVM start-up per attempt, SES import
 * and the board save.
 */
export const AUTOROUTE_OVERHEAD_MS = 120_000;

/**
 * `download_jlcpcb_database` fetches a 230-400 MB catalog and converts it to
 * SQLite: 30 s at best, many minutes on a slow link (one 50 MiB volume took
 * about 8 minutes for the reporter of #454). Under the 30 s default the bridge
 * gave up and restarted the worker mid-download, which cut the files short.
 */
export const JLCPCB_DOWNLOAD_TIMEOUT_MS = 3_600_000;

/** Slack added to GUI tools that take their own `timeout` (seconds) parameters. */
export const GUI_WAIT_OVERHEAD_MS = 30_000;

/**
 * `fab_package` runs several kicad-cli exports, a netlist export, DRC and ERC
 * back to back (MCP_ISSUES F3); on a large board that is minutes.
 */
export const FAB_PACKAGE_TIMEOUT_MS = 1_800_000;

/** Process start, board load and save around a `run_pcbnew_script` body. */
export const SCRIPT_OVERHEAD_MS = 60_000;

type Env = Record<string, string | undefined>;

/**
 * How long the Node side waits for `command` before abandoning the request.
 *
 * `autoroute` is computed rather than fixed: its Python side takes a
 * per-attempt `timeout` (seconds, default 300) and repeats it `attempts` times
 * for best-of-N, so a flat ceiling would still cut off a caller who legitimately
 * raised either. Under-waiting here is what issue #251 reports — the Node
 * default fired at 30 s while Freerouting was still working, so the tool
 * reported failure even though a valid .ses had been written.
 *
 * The GUI tools that already take a `timeout` (and `runTimeout`) in seconds get
 * the same treatment: the per-call parameter extends the bridge timeout, so
 * `kicad_run_drc` (15 s + 60 s by default) is no longer cut off at 30 s.
 */
export function computeCommandTimeout(
  command: string,
  params?: unknown,
  env: Env = process.env,
): number {
  const override = parseToolTimeoutOverrides(env.KICAD_MCP_TOOL_TIMEOUTS)[command];
  if (override !== undefined) return override;

  const p = (params ?? {}) as Record<string, unknown>;

  if (command === "autoroute") {
    const perAttemptSec = toPositiveNumber(p.timeout) ?? 300;
    const attempts = Math.max(1, Math.floor(toPositiveNumber(p.attempts) ?? 1));

    const budgetMs = perAttemptSec * 1000 * attempts + AUTOROUTE_OVERHEAD_MS;
    // Never drop below the blanket long-running allowance.
    return Math.max(budgetMs, LONG_COMMAND_TIMEOUT_MS);
  }

  if (command === "kicad_gui_wait_for") {
    const waitSec = toPositiveNumber(p.timeout) ?? 10;
    return Math.max(waitSec * 1000 + GUI_WAIT_OVERHEAD_MS, defaultTimeout(env));
  }

  if (command === "kicad_run_drc") {
    const dialogSec = toPositiveNumber(p.timeout) ?? 15;
    const runSec = toPositiveNumber(p.runTimeout) ?? 60;
    return Math.max((dialogSec + runSec) * 1000 + GUI_WAIT_OVERHEAD_MS, MEDIUM_COMMAND_TIMEOUT_MS);
  }

  if (command === "download_jlcpcb_database") {
    return JLCPCB_DOWNLOAD_TIMEOUT_MS;
  }

  if (command === "fab_package") {
    return FAB_PACKAGE_TIMEOUT_MS;
  }

  if (command === "run_pcbnew_script") {
    // The Python side kills the script at `timeout` seconds (default 60).
    const scriptSec = Math.min(toPositiveNumber(p.timeout) ?? 60, 1800);
    return Math.max(scriptSec * 1000 + SCRIPT_OVERHEAD_MS, MEDIUM_COMMAND_TIMEOUT_MS);
  }

  // Board edits with `drcDelta` save and run a full DRC (two when the last
  // recorded run is stale), so they get the check budget.
  if (p.drcDelta === true) {
    return CHECK_COMMAND_TIMEOUT_MS;
  }
  if ((CHECK_COMMANDS as readonly string[]).includes(command)) {
    return CHECK_COMMAND_TIMEOUT_MS;
  }
  if ((LONG_RUNNING_COMMANDS as readonly string[]).includes(command)) {
    return LONG_COMMAND_TIMEOUT_MS;
  }
  if ((MEDIUM_COMMANDS as readonly string[]).includes(command)) {
    return Math.max(MEDIUM_COMMAND_TIMEOUT_MS, defaultTimeout(env));
  }

  return defaultTimeout(env);
}

/** The default timeout, honouring `KICAD_MCP_COMMAND_TIMEOUT_MS`. */
export function defaultTimeout(env: Env = process.env): number {
  return toPositiveNumber(env.KICAD_MCP_COMMAND_TIMEOUT_MS) ?? DEFAULT_COMMAND_TIMEOUT_MS;
}

/** Grace period after a timeout before a still-busy worker is restarted. */
export function computeTimeoutGrace(env: Env = process.env): number {
  return toPositiveNumber(env.KICAD_MCP_TIMEOUT_GRACE_MS) ?? DEFAULT_TIMEOUT_GRACE_MS;
}

/**
 * Parse `KICAD_MCP_TOOL_TIMEOUTS` ("run_drc=1200000, batch_connect=900000").
 * Malformed entries are ignored rather than failing start-up.
 */
export function parseToolTimeoutOverrides(raw: string | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  if (!raw) return out;
  for (const entry of raw.split(/[,;]/)) {
    const [name, value] = entry.split("=").map((s) => s?.trim());
    const ms = toPositiveNumber(value);
    if (name && ms !== undefined) out[name] = ms;
  }
  return out;
}

/** Finite, positive numbers only; anything else falls back to the default. */
function toPositiveNumber(value: unknown): number | undefined {
  const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return undefined;
  return n;
}
