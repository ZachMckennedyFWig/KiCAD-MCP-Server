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
      timeout: z
        .number()
        .optional()
        .describe(
          "Per-attempt timeout in seconds (default: 300). Freerouting is killed when it is reached and nothing it routed is kept, so the router is given a shorter time budget of its own (see jobTimeout).",
        ),
      jobTimeout: z
        .number()
        .min(0)
        .optional()
        .describe(
          "Time in seconds Freerouting may spend routing before it stops by itself and writes what it has routed (--router.job_timeout). Default: `timeout` minus a tenth of it (at least 60 s, at most 120 s); no default budget with a `timeout` under 120 s, where it would cut off runs that finish in time. Needs Freerouting 2.4 or later: older versions stop at the budget but write no session file. The result has time_budget_reached: true when routing stopped this way; the board then holds a partial routing. 0 passes no time budget.",
        ),
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
      routerSettings: z
        .record(
          z.string(),
          z.union([
            z.string(),
            z.number(),
            z.boolean(),
            z.array(z.union([z.string(), z.number(), z.boolean()])),
          ]),
        )
        .optional()
        .describe(
          'Freerouting router settings for this run, by the names in Freerouting\'s docs/settings.md under `router`; each is passed as --router.<name>=<value>, a list joined with commas. Examples: {"autorouter.priority_net_classes": ["DIFF", "ANALOG"]} routes those net classes first and keeps other nets from ripping them; {"autorouter.plane_as_priority": true} gives each plane pad a via of its own into its plane (the plane\'s layer must be of type power); {"autorouter.ignore_net_classes": ["HV"]}. Which settings exist depends on the Freerouting version: priority_net_classes and plane_as_priority are on Freerouting master only (merged 2026-11-07), not in a v2 release. Freerouting skips a setting it does not know and keeps routing; the reply lists those in router_settings_rejected. Use maxPasses and jobTimeout for those two. With attempts above 1 the thread count (max_threads, autorouter.max_threads, optimizer.max_threads) is refused: best-of-N runs single-threaded so the attempts stay comparable.',
        ),
      routeNets: z
        .array(z.string())
        .optional()
        .describe(
          "Route only these nets (names or globs such as '/HV PWR/*'). Other nets stay on the board as obstacles but are left out of the routing job, so a board can be routed one isolation domain at a time.",
        ),
      excludeNets: z
        .array(z.string())
        .optional()
        .describe(
          "Leave these nets (names or globs) out of the routing job, e.g. high-current nets you will pour by hand.",
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
      layerTraceCosts: z
        .record(z.string(), z.number())
        .optional()
        .describe(
          "Per-layer trace cost factor, e.g. {\"In1.Cu\": 5}: the layer stays usable but a track there costs more (the factor along its preferred direction, twice that against it; Freerouting's defaults are about 1 and 2), so a plane layer carries only the tracks that fit nowhere else. Keys are canonical (In1.Cu) or the board's own layer names; a name the board lacks is refused. Combines with signalLayers; written to the same Freerouting rules file.",
        ),
      signalLayers: z
        .array(z.string())
        .optional()
        .describe(
          'Copper layers tracks may use, e.g. ["F.Cu", "In2.Cu", "B.Cu"] when In1 is a plane; canonical names or the board\'s own (a renamed F.Cu is found either way), and a name the board lacks is refused. The other copper layers get (active off) in a Freerouting rules file (autoroute_settings layer_rule, passed with -dr): no tracks there, vias still pass through, so a plane is not cut into islands. Nothing else is written to the file, so Freerouting\'s own per-layer defaults stay in force. The reply lists the restricted layers by the board\'s names in tracksKeptOffLayers. Default: all layers.',
        ),
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
