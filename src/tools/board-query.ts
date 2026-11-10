/**
 * Board query and inspection tools (MCP_ISSUES Part 2, section 2.4: Q1, Q3, Q4).
 *
 * All four are read-only and answer from the board loaded in memory, or from
 * boardPath (loaded fresh from disk) when given. Units are millimetres in
 * KiCad board coordinates. Outputs are compact by default (counts plus the
 * first `limit` rows); `detail` asks for more.
 *
 * The cropped / net-highlighted render (Q2) is an extension of
 * get_board_2d_view in board.ts (region, highlightNets, scale, ...).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const COORDS =
  "Units: mm, KiCad board coordinates (absolute positions as stored in the .kicad_pcb, +y down; not relative to a grid or aux origin).";

const boardPath = z
  .string()
  .optional()
  .describe(
    "Absolute path to a .kicad_pcb to read fresh from disk. Omit to use the board loaded in memory (unsaved edits included).",
  );

const detail = z
  .enum(["summary", "items", "full"])
  .optional()
  .describe(
    "summary = counts only; items = counts + compact rows; full = rows + uuids and extra fields",
  );

const limit = z.number().int().min(0).optional().describe("Maximum rows returned");

// Unindented JSON: these tools exist to save tokens, and indentation roughly
// doubles the size of a row table.
function asText(result: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
}

export function registerBoardQueryTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "query_region",
    [
      "List what is inside a window of the board: pads, vias, tracks, arcs, zones, rule areas and text",
      "(footprints and graphics on request), as a compact table.",
      COORDS,
      'Returns {count, byKind, nets, columns:["kind","id","net","layer","geom","info","locked"], rows, truncated?, omitted?}.',
      "geom is [x,y] for pads/vias/text, [x1,y1,x2,y2] for tracks, [x1,y1,xm,ym,x2,y2] for arcs, [x,y,rot] for footprints",
      "and the outline bbox for zones. id is REF.PAD for pads and the first 8 uuid characters for tracks/vias",
      '(connected_items accepts that prefix). info holds size/width ("rect 0.28x0.665", "w0.2", "0.6/0.3" via dia/drill),',
      "or for zones whether fill exists inside the window. Items are included when their copper touches the window.",
      'detail "summary" returns counts by kind, layer and net only. Default: detail "items", limit 60.',
    ].join(" "),
    {
      x0: z.number().describe("Window corner X (mm)"),
      y0: z.number().describe("Window corner Y (mm)"),
      x1: z.number().describe("Opposite corner X (mm)"),
      y1: z.number().describe("Opposite corner Y (mm)"),
      layers: z
        .array(z.string())
        .optional()
        .describe(
          'Only items on these layers, e.g. ["F.Cu","In1.Cu"] (board or legacy names accepted)',
        ),
      kinds: z
        .array(
          z.enum([
            "pad",
            "via",
            "track",
            "arc",
            "zone",
            "rule_area",
            "text",
            "footprint",
            "graphic",
          ]),
        )
        .optional()
        .describe("Item kinds to include (default: pad, via, track, arc, zone, rule_area, text)"),
      nets: z
        .array(z.string())
        .optional()
        .describe(
          "Only items on these nets. A name without '/' also matches the last path component (SDA matches /MCU/SDA). Text and graphics are dropped when filtering by net.",
        ),
      detail,
      limit,
      boardPath,
    },
    async (args: Record<string, unknown>) => asText(await callKicadScript("query_region", args)),
  );

  server.tool(
    "net_report",
    [
      "Per-net routing report in one call: track length per layer, via count and sizes, layers used, track width min/max,",
      "island count / fully-connected flag (from KiCad's connectivity), plane-split crossings and reference voids under",
      "the net's tracks, and named regions the net enters.",
      COORDS,
      "Split crossings: for each track the nearest copper layers above/below that carry zones are its reference planes;",
      "a crossing is where the zone net under the track changes (e.g. GND -> +3V3 on In2.Cu); a void is a stretch of at",
      "least minVoidMm with no plane copper under the track.",
      "Returns {netCount, notFullyConnected, planeLayers, netsWithSplitCrossings, nets:[{net, pads, tracks, vias, zones,",
      "lengthMm, islands, fullyConnected, unroutedConnections, splitCrossings, referenceVoids, lengthByLayer?, layers?,",
      "zoneLayers?, widthMm?, viaSizes?, crossings?:[{at,layer,refLayer,from,to}], voids?, regions?}], warnings?}.",
      "With nets: default detail is items (crossing lists capped at 5 per net; full = all). Without nets: every net,",
      "detail summary, problems first, limit 25.",
    ].join(" "),
    {
      nets: z
        .array(z.string())
        .optional()
        .describe(
          "Net names (a name without '/' may match a hierarchical net's last component). Omit for all nets.",
        ),
      detail,
      limit: limit.describe("Maximum nets returned (default 25)"),
      planeCrossings: z
        .boolean()
        .optional()
        .describe("Compute split crossings and voids (default true)"),
      referenceLayers: z
        .array(z.string())
        .optional()
        .describe(
          "Restrict which copper layers count as reference planes (default: every layer that has zones)",
        ),
      minVoidMm: z
        .number()
        .optional()
        .describe("Shortest plane-free stretch reported as a void (default 1.0 mm)"),
      regions: z
        .record(
          z.union([
            z.array(z.tuple([z.number(), z.number()])),
            z.object({ x0: z.number(), y0: z.number(), x1: z.number(), y1: z.number() }),
          ]),
        )
        .optional()
        .describe(
          'Named regions, {"AUDIO": [[x,y],...] or {x0,y0,x1,y1}}; each net reports the regions it enters',
        ),
      boardPath,
    },
    async (args: Record<string, unknown>) => asText(await callKicadScript("net_report", args)),
  );

  server.tool(
    "connected_items",
    [
      "Find the copper island an item or a point belongs to, using KiCad's connectivity engine.",
      "Start from uuid (full or a unique prefix of 8+ characters, as query_region shows), ref + pad, or x + y",
      "(optionally layer; a point picks pad > via > track > zone fill).",
      COORDS,
      "Returns {start, otherHits?, net, netIslandCount, isWholeNet, island:{island, pads, tracks, vias, zoneFills,",
      'layers, bbox, padList}}; detail "items"/"full" adds the island\'s items as query_region-style rows.',
      "If netIslandCount > 1 the net is split: call why_unconnected for the gaps.",
    ].join(" "),
    {
      uuid: z
        .string()
        .optional()
        .describe("Pad, track, via or zone uuid (or unique prefix, >= 8 chars)"),
      ref: z.string().optional().describe("Footprint reference, used with pad"),
      pad: z.string().optional().describe("Pad number on ref, e.g. '5'"),
      x: z.number().optional().describe("Point X (mm)"),
      y: z.number().optional().describe("Point Y (mm)"),
      layer: z
        .string()
        .optional()
        .describe("Copper layer for the point (and to pick a zone layer)"),
      detail: detail.describe(
        "summary (default) = island summary; items/full = also list the island's items",
      ),
      limit: limit.describe("Maximum rows / pads listed (default 40)"),
      boardPath,
    },
    async (args: Record<string, unknown>) => asText(await callKicadScript("connected_items", args)),
  );

  server.tool(
    "why_unconnected",
    [
      "Explain why a net is not fully connected: its copper islands (pads, tracks, vias and zone fills per island,",
      "biggest first) and the nearest gap between islands with coordinates.",
      COORDS,
      "Gaps are copper-edge to copper-edge distances measured on a shared layer; when two islands share no layer the",
      "2D gap is given with needsVia: true. The gaps form a minimum spanning tree, so closing all of them connects the net.",
      "Returns {net, fullyConnected, islandCount, islands:[{island, pads, tracks, vias, zoneFills, layers, zoneLayers?,",
      "bbox, padList}], gaps?:[{fromIsland, toIsland, gapMm, from:{at:[x,y], kind, id, layers}, to:{...}, layer? | needsVia?}],",
      "notes?}. Notes flag unfilled zones (run refill_zones) and isolated fill fragments.",
    ].join(" "),
    {
      net: z
        .string()
        .describe("Net name, e.g. GND or /Audio/LRCK (a unique last path component also works)"),
      limit: limit.describe("Maximum islands and gaps listed (default 20)"),
      boardPath,
    },
    async (args: Record<string, unknown>) => asText(await callKicadScript("why_unconnected", args)),
  );
}
