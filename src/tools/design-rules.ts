/**
 * Design rules tools for KiCAD MCP server
 *
 * These tools handle design rule checking and configuration
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { formatKicadResult } from "./tool-response.js";

// Command function type for KiCAD script calls
type CommandFunction = (command: string, params: Record<string, unknown>) => Promise<any>;

/**
 * Output-shaping parameters shared by run_drc and run_erc (MCP_ISSUES Q5):
 * summaries first, detail on request, filters, and a delta against the
 * previous run.
 */
export const checkOutputParams = {
  detail: z
    .enum(["summary", "items", "full"])
    .optional()
    .describe(
      "summary (default): counts by type/severity + the first `limit` items with coordinates (mm). items: every matching item. full: also the legacy `violations` list with item UUIDs.",
    ),
  limit: z
    .number()
    .int()
    .min(0)
    .optional()
    .describe("Max items listed (default 20 for summary, all for items/full)"),
  ignore: z
    .array(z.string())
    .optional()
    .describe("Violation-type globs to drop, e.g. ['silk_*', 'lib_symbol_mismatch']"),
  nets: z
    .array(z.string())
    .optional()
    .describe("Only violations naming one of these nets ('+5V' also matches '/Power/+5V')"),
  refs: z
    .array(z.string())
    .optional()
    .describe("Only violations naming one of these references, e.g. ['U3', 'C12']"),
  region: z
    .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
    .optional()
    .describe("Only violations with an item inside this box (mm)"),
  timeoutSec: z
    .number()
    .int()
    .optional()
    .describe("kicad-cli timeout in seconds (default 600 for DRC, 300 for ERC; max 1800)"),
};

/** Render a compact check result (counts, top items, delta) as a few text lines. */
export function formatCheckSummary(label: string, result: any): string {
  const counts = result.counts ?? { total: result.summary?.total ?? 0, by_severity: {} };
  const sev = counts.by_severity ?? {};
  const lines: string[] = [];
  let head = `${label}: ${counts.total} violation(s) — ${sev.error ?? 0} error, ${sev.warning ?? 0} warning`;
  if (result.filters && result.unfilteredTotal !== undefined) {
    head += ` (filtered from ${result.unfilteredTotal})`;
  }
  lines.push(head);
  const delta = result.delta;
  if (delta && !delta.firstRun) {
    lines.push(
      `Since last run (${delta.previousRunAt ?? "?"}): +${delta.newCountAll} new, -${delta.resolvedCountAll} resolved`,
    );
  } else if (delta?.firstRun) {
    lines.push("No previous run recorded for this file (delta starts next run).");
  }
  const byType = Object.entries(counts.by_type ?? {}) as Array<[string, number]>;
  if (byType.length > 0) {
    lines.push("By type: " + byType.map(([t, n]) => `${t} ${n}`).join(", "));
  }
  const fmt = (v: any) => {
    const loc =
      v.location && v.location.x !== undefined ? ` @ (${v.location.x}, ${v.location.y})` : "";
    const items = Array.isArray(v.items) && v.items.length ? ` — ${v.items.join("; ")}` : "";
    return `[${v.severity}] ${v.type}: ${v.message}${loc}${items}`;
  };
  const items: any[] = result.items ?? [];
  if (items.length > 0) {
    lines.push("");
    items.forEach((v, i) => lines.push(`${i + 1}. ${fmt(v)}`));
    if (result.itemsTruncated) {
      lines.push(`... ${counts.total - items.length} more (raise limit or use detail='items')`);
    }
  }
  if (delta && !delta.firstRun) {
    for (const [name, list] of [
      ["New", delta.new],
      ["Resolved", delta.resolved],
    ] as Array<[string, any[]]>) {
      if (list?.length) {
        lines.push("", `${name}:`);
        list.forEach((v) => lines.push(`  ${fmt(v)}`));
      }
    }
  }
  if (result.rules?.warning) lines.push("", `Rules: ${result.rules.warning}`);
  else if (result.rules?.note) lines.push("", `Rules: ${result.rules.note}`);
  return lines.join("\n");
}

/**
 * Register design rule tools with the MCP server
 *
 * @param server MCP server instance
 * @param callKicadScript Function to call KiCAD script commands
 */
export function registerDesignRuleTools(server: McpServer, callKicadScript: CommandFunction): void {
  logger.info("Registering design rule tools");

  // ------------------------------------------------------
  // Set Design Rules Tool
  // ------------------------------------------------------
  server.tool(
    "set_design_rules",
    "Configure PCB design rules: clearance, track width, via dimensions and courtyard requirements.",
    {
      clearance: z.number().optional().describe("Minimum clearance between copper items (mm)"),
      trackWidth: z.number().optional().describe("Default track width (mm)"),
      viaDiameter: z.number().optional().describe("Default via diameter (mm)"),
      viaDrill: z.number().optional().describe("Default via drill size (mm)"),
      microViaDiameter: z.number().optional().describe("Default micro via diameter (mm)"),
      microViaDrill: z.number().optional().describe("Default micro via drill size (mm)"),
      minTrackWidth: z.number().optional().describe("Minimum track width (mm)"),
      minViaDiameter: z.number().optional().describe("Minimum via diameter (mm)"),
      minViaDrill: z.number().optional().describe("Minimum via drill size (mm)"),
      minMicroViaDiameter: z.number().optional().describe("Minimum micro via diameter (mm)"),
      minMicroViaDrill: z.number().optional().describe("Minimum micro via drill size (mm)"),
      minHoleDiameter: z.number().optional().describe("Minimum hole diameter (mm)"),
      requireCourtyard: z
        .boolean()
        .optional()
        .describe("Whether to require courtyards for all footprints"),
      courtyardClearance: z
        .number()
        .optional()
        .describe("Minimum clearance between courtyards (mm)"),
    },
    async (params) => {
      logger.debug("Setting design rules");
      const result = await callKicadScript("set_design_rules", params);

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get Design Rules Tool
  // ------------------------------------------------------
  server.tool(
    "get_design_rules",
    "Return the current PCB design rules (clearance, track width, via sizes, courtyard settings).",
    {},
    async () => {
      logger.debug("Getting design rules");
      const result = await callKicadScript("get_design_rules", {});

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Run DRC Tool
  // ------------------------------------------------------
  server.tool(
    "run_drc",
    "Run the KiCAD Design Rule Check (DRC) on the saved PCB with kicad-cli, against the project's own .kicad_pro/.kicad_dru (`rules` says which were used). Includes unconnected items and, on request, schematic-parity issues. Compact by default: counts by type/severity plus the first `limit` violations with coordinates (mm), and a `delta` (new/resolved) against the previous run on this board. Use `ignore`/`nets`/`refs`/`region` to narrow, detail='full' for everything. The full list is also saved to `violationsFile`. Zones are refilled first by default, as in KiCad's DRC dialog.",
    {
      reportPath: z.string().optional().describe("Optional path to save the DRC report"),
      refillZones: z
        .boolean()
        .optional()
        .describe(
          "Refill all zones before checking (default true, like the DRC dialog). Stale fills report clearance errors that a refill would clear.",
        ),
      schematicParity: z
        .boolean()
        .optional()
        .describe("Also check the board against the project's schematic (default false)"),
      projectPath: z
        .string()
        .optional()
        .describe(
          ".kicad_pro (or its directory) whose rules to apply. Default: the board's own project, else the only .kicad_pro next to the board.",
        ),
      ...checkOutputParams,
    },
    async (args) => {
      logger.debug("Running DRC check");
      const result = await callKicadScript("run_drc", args);

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Assign Net to Class Tool
  // ------------------------------------------------------
  server.tool(
    "assign_net_to_class",
    "Assign a net to an existing net class to apply its specific design rules.",
    {
      net: z.string().describe("Name of the net"),
      netClass: z.string().describe("Name of the net class"),
    },
    async ({ net, netClass }) => {
      logger.debug(`Assigning net ${net} to class ${netClass}`);
      const result = await callKicadScript("assign_net_to_class", {
        net,
        netClass,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Set Layer Constraints Tool
  // ------------------------------------------------------
  server.tool(
    "set_layer_constraints",
    "Set per-layer design rule constraints (minimum track width, clearance and via dimensions).",
    {
      layer: z.string().describe("Layer name (e.g., 'F.Cu')"),
      minTrackWidth: z.number().optional().describe("Minimum track width for this layer (mm)"),
      minClearance: z.number().optional().describe("Minimum clearance for this layer (mm)"),
      minViaDiameter: z.number().optional().describe("Minimum via diameter for this layer (mm)"),
      minViaDrill: z.number().optional().describe("Minimum via drill size for this layer (mm)"),
    },
    async ({ layer, minTrackWidth, minClearance, minViaDiameter, minViaDrill }) => {
      logger.debug(`Setting constraints for layer: ${layer}`);
      const result = await callKicadScript("set_layer_constraints", {
        layer,
        minTrackWidth,
        minClearance,
        minViaDiameter,
        minViaDrill,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Check Clearance Tool
  // ------------------------------------------------------
  server.tool(
    "check_clearance",
    "Check the actual clearance between two PCB items (track, via, pad, zone or component) and report whether it meets the design rules.",
    {
      item1: z
        .object({
          type: z
            .enum(["track", "via", "pad", "zone", "component"])
            .describe("Type of the first item"),
          id: z.string().optional().describe("ID of the first item (if applicable)"),
          reference: z.string().optional().describe("Reference designator (for component)"),
          position: z
            .object({
              x: z.number().optional(),
              y: z.number().optional(),
              unit: z.enum(["mm", "mil", "inch"]).optional(),
            })
            .optional()
            .describe("Position to check (if ID not provided)"),
        })
        .describe("First item to check"),
      item2: z
        .object({
          type: z
            .enum(["track", "via", "pad", "zone", "component"])
            .describe("Type of the second item"),
          id: z.string().optional().describe("ID of the second item (if applicable)"),
          reference: z.string().optional().describe("Reference designator (for component)"),
          position: z
            .object({
              x: z.number().optional(),
              y: z.number().optional(),
              unit: z.enum(["mm", "mil", "inch"]).optional(),
            })
            .optional()
            .describe("Position to check (if ID not provided)"),
        })
        .describe("Second item to check"),
    },
    async ({ item1, item2 }) => {
      logger.debug(`Checking clearance between ${item1.type} and ${item2.type}`);
      const result = await callKicadScript("check_clearance", {
        item1,
        item2,
      });

      return formatKicadResult(result);
    },
  );

  // ------------------------------------------------------
  // Get DRC Violations Tool
  // ------------------------------------------------------
  server.tool(
    "get_drc_violations",
    "Return the list of current DRC violations on the PCB, optionally filtered by severity (error, warning).",
    {
      severity: z
        .enum(["error", "warning", "all"])
        .optional()
        .describe("Filter violations by severity"),
    },
    async ({ severity }) => {
      logger.debug("Getting DRC violations");
      const result = await callKicadScript("get_drc_violations", { severity });

      return formatKicadResult(result);
    },
  );

  logger.info("Design rule tools registered");
}
