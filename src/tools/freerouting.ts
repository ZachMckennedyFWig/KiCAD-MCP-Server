/**
 * Freerouting autoroute tools for KiCAD MCP server
 *
 * Provides autorouting via Freerouting (Specctra DSN/SES workflow).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

export function registerFreeroutingTools(server: McpServer, callKicadScript: Function) {
  // Full autoroute: export DSN -> run Freerouting -> import SES
  //
  // Best-of-N support (the `attempts` / `targetNets` / `passSchedule`
  // parameters) is ported from morningfire-pcb-automation:
  //   https://github.com/NiNjA-CodE/morningfire-pcb-automation
  //   (scripts/routing/freeroute_runner.py — `score_ses` + run loop)
  // On dense boards a single attempt regularly leaves 1–7 nets unrouted;
  // cycling through a few `--max-passes` values typically drives the
  // unrouted count to zero.
  server.tool(
    "autoroute",
    "Run Freerouting autorouter on the current PCB. Exports to Specctra DSN, runs Freerouting CLI, and imports the routed SES result. Requires freerouting.jar and the Java version it was built for (21+; Freerouting 2.4.x needs 25), or Docker/Podman (see check_freerouting). Set `attempts` > 1 to run best-of-N: Freerouting is invoked multiple times with varied `--max-passes`, each result is scored by (nets_routed * 1000 + segments, +50000 bonus when every `targetNets` entry routed), and the winning SES is imported. Single-attempt behaviour is unchanged when `attempts` is omitted. Orchestrated mode (any of async, nets, lockExisting, stripRuleAreas, forbidRegionsForNets, passes, stallTimeout, importMode, evaluate, lockRouted): the in-memory board is staged to a temp copy, the DSN is exported from that disk copy and rewritten (pour-only keepouts stripped, subset/locking), Freerouting's log is streamed (stall detection), the SES is imported into a copy, zones are refilled and KiCad DRC evaluates it; the board file is replaced only if accepted (importMode) and unchanged since the start. Returns {pipeline:'orchestrated', freerouting:{attempts:[{unroutedReportedByFreerouting, stopReason,...}]}, kicad:{before:{unconnected, unconnectedByNet, drcErrors}, after:{...}}, drcDelta:{newCount, resolvedCount, new, resolved}, decision:{import, reason}, applied, logTail, dsn:{ruleAreas, netFilter, lockedWiring}} or, with async, {jobId}.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      freeroutingJar: z
        .string()
        .optional()
        .describe(
          "Path to freerouting.jar (default: ~/.kicad-mcp/freerouting.jar or FREEROUTING_JAR env)",
        ),
      maxPasses: z
        .number()
        .optional()
        .describe(
          "Maximum routing passes for single-attempt mode (default: 20). Ignored when `attempts` > 1; use `passSchedule` instead.",
        ),
      timeout: z.number().optional().describe("Per-attempt timeout in seconds (default: 300)"),
      attempts: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe(
          "Number of Freerouting runs to try (default: 1 — backward-compatible). When > 1, runs best-of-N: scores each attempt by routing completeness and keeps the SES with the highest score. Recommended: 3–5 for dense boards.",
        ),
      targetNets: z
        .array(z.string())
        .optional()
        .describe(
          "Optional list of critical net names. An attempt that routes all of them earns a 50,000-point scoring bonus, breaking ties in favour of designs that include the must-have nets.",
        ),
      passSchedule: z
        .array(z.number())
        .optional()
        .describe(
          "Per-attempt `--max-passes` values to cycle through (default: [50, 60, 65, 70, 75, 80, 85, 90, 55, 95]). The list wraps if `attempts` exceeds its length.",
        ),
      keepArtifacts: z
        .boolean()
        .optional()
        .describe(
          "Keep the intermediate .dsn/.ses files next to the board after the run (default: false). Artifacts are staged in a temporary directory and removed on every exit — success, failure, or crash — unless this is set.",
        ),
      // Orchestration options (MCP_ISSUES R2). Giving any of these switches to
      // the orchestrated pipeline (python/commands/autoroute_pipeline.py).
      async: z
        .boolean()
        .optional()
        .describe(
          "Run in the background and return {jobId} at once; poll job_status, then call job_apply to write an accepted result. The board is unchanged until job_apply.",
        ),
      nets: z
        .array(z.string())
        .optional()
        .describe(
          "Route only these nets. Other nets keep their pads and copper as fixed obstacles, and only these nets' routing is imported.",
        ),
      lockExisting: z
        .boolean()
        .optional()
        .describe(
          "Export all existing tracks/vias as fixed so Freerouting cannot move or rip them up (default false). Lock flags on the board are restored after import.",
        ),
      stripRuleAreas: z
        .enum(["pour_only", "all", "none"])
        .optional()
        .describe(
          "Remove rule-area keepouts from the DSN (the board keeps them). 'pour_only' (default): rule areas that only forbid pours, which KiCad otherwise exports as hard obstacles; 'all': every rule area; 'none'.",
        ),
      forbidRegionsForNets: z
        .array(
          z.object({
            nets: z.array(z.string()),
            region: z
              .object({
                x1: z.number().optional(),
                y1: z.number().optional(),
                x2: z.number().optional(),
                y2: z.number().optional(),
                polygon: z.array(z.object({ x: z.number(), y: z.number() })).optional(),
              })
              .describe("Rectangle {x1,y1,x2,y2} or {polygon} in mm"),
            layers: z.array(z.string()).optional(),
          }),
        )
        .optional()
        .describe(
          "Keep these nets out of a region. When every routed net is listed the region becomes a hard keepout in the DSN; otherwise a listed net whose new copper enters the region is not imported (reported in forbiddenRegionRejected).",
        ),
      passes: z
        .number()
        .optional()
        .describe(
          "Freerouting max passes (-mp) for the orchestrated run (default maxPasses or 20)",
        ),
      stallTimeout: z
        .number()
        .optional()
        .describe(
          "Stop an attempt that makes no progress (no better pass score, no fewer unrouted, no new stage) for this many seconds. Freerouting writes no SES when stopped.",
        ),
      importMode: z
        .enum(["always", "if_better"])
        .optional()
        .describe(
          "'if_better' (default): import only when KiCad's own unconnected count drops without more DRC errors (or errors drop at equal count); 'always': import any result.",
        ),
      evaluate: z
        .boolean()
        .optional()
        .describe(
          "Run kicad-cli DRC (zones refilled) before and after to report KiCad's unconnected count and the DRC delta (default true)",
        ),
      lockRouted: z
        .boolean()
        .optional()
        .describe("Lock the newly routed tracks/vias (default false)"),
    },
    async (args: any) => {
      const result = await callKicadScript("autoroute", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Export DSN only
  server.tool(
    "export_dsn",
    "Export the current PCB to Specctra DSN format. Useful for manual Freerouting workflow or external autorouters.",
    {
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
      outputPath: z
        .string()
        .optional()
        .describe("Output DSN file path (default: same dir as board)"),
    },
    async (args: any) => {
      const result = await callKicadScript("export_dsn", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Import SES
  server.tool(
    "import_ses",
    "Import a Specctra SES (session) file into the current PCB. Use after running Freerouting externally.",
    {
      sesPath: z.string().describe("Path to the .ses file to import"),
      boardPath: z.string().optional().describe("Path to .kicad_pcb file (default: current board)"),
    },
    async (args: any) => {
      const result = await callKicadScript("import_ses", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );

  // Check Freerouting dependencies
  server.tool(
    "check_freerouting",
    "Check if Java and Freerouting JAR are available on the system. Run this before autoroute to verify prerequisites.",
    {
      freeroutingJar: z.string().optional().describe("Path to freerouting.jar to check"),
    },
    async (args: any) => {
      const result = await callKicadScript("check_freerouting", args);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(result, null, 2),
          },
        ],
      };
    },
  );
}
