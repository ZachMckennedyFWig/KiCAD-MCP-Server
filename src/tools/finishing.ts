/**
 * Board finishing tools and reference-layout transplant (MCP_ISSUES §2.6 R4, §2.7 F1/F2/F4).
 *
 * All coordinates and sizes are millimetres in board coordinates (origin at the
 * top-left of the sheet, y grows downwards). Every tool works on the loaded
 * board, supports `dryRun`, leaves the board unchanged when it fails, and
 * auto-saves only after a committed change.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

const point = z.union([
  z.tuple([z.number(), z.number()]),
  z.object({ x: z.number(), y: z.number() }),
]);

const region = z
  .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
  .describe("Rectangle in mm (any two opposite corners)");

function asText(result: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
}

export function registerFinishingTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "stitch_ground",
    "Place stitching vias for a pour net (default GND): a grid inside the net's pours, a ring along Edge.Cuts and via fences along polylines or a zone outline. " +
      "Each via is checked against all other-net copper on every layer (net-class clearance + keepClear), hole-to-hole spacing, via keepouts, courtyards (courtyardPolicy) and the board edge, " +
      "and is placed only where the net's filled pour covers the whole via on at least minLayers copper layers (and on every layer in requireLayers). Zones are refilled afterwards. " +
      "Units mm. Returns {placed:{fence,edge,grid,total}, candidates, rejected:{reason:count}, vias:[{x,y,kind}] (first `limit`), pourLayers, clearance, minSpacing, saved}.",
    {
      net: z.string().optional().describe("Pour net (default: GND / GROUND / VSS / /GND / AGND)"),
      grid: z
        .number()
        .optional()
        .describe(
          "Grid pitch in mm (default 2.5; 0 = no grid). Grid points must still fall in the pour.",
        ),
      edgePitch: z
        .number()
        .optional()
        .describe("Pitch of the edge ring along Edge.Cuts in mm (default 2.0; 0 = no ring)"),
      edgeInset: z
        .number()
        .optional()
        .describe(
          "Distance of the edge ring from Edge.Cuts in mm (default: copper-edge clearance + via radius + 0.25)",
        ),
      fencePolylines: z
        .array(
          z.union([
            z.array(point),
            z.object({
              points: z.array(point).optional(),
              closed: z.boolean().optional(),
              zoneName: z.string().optional(),
            }),
          ]),
        )
        .optional()
        .describe(
          "Via fences: each entry is [[x,y],...] (mm), {points, closed} or {zoneName} (fence along that zone/rule-area outline)",
        ),
      fencePitch: z.number().optional().describe("Fence via pitch in mm (default 1.5)"),
      fenceNudge: z
        .number()
        .optional()
        .describe(
          "If a fence spot is blocked, also try this far either side of the line, mm (default 0.4)",
        ),
      keepClear: z
        .number()
        .optional()
        .describe(
          "Extra clearance to other-net copper on top of the net-class clearance, mm (default 0.1)",
        ),
      minSpacing: z
        .number()
        .optional()
        .describe(
          "Minimum centre distance to other vias of the net, mm (default max(via+clearance, drill+hole-to-hole))",
        ),
      minLayers: z
        .number()
        .int()
        .optional()
        .describe(
          "Pour of the net must cover the via on at least this many copper layers (default 2)",
        ),
      requireLayers: z
        .array(z.string())
        .optional()
        .describe('Layers where the pour must be present at every via, e.g. ["F.Cu","B.Cu"]'),
      courtyardPolicy: z
        .enum(["avoid", "allow", "fence_allowed"])
        .optional()
        .describe(
          "Courtyards: avoid for all vias, allow everywhere, or fence_allowed (default: grid/edge avoid, fences may enter)",
        ),
      via: z
        .object({ size: z.number().optional(), drill: z.number().optional() })
        .optional()
        .describe("Via geometry in mm (default {size: 0.6, drill: 0.3})"),
      region: region.optional().describe("Only place vias inside this rectangle (mm)"),
      lock: z.boolean().optional().describe("Lock the new vias (default true)"),
      refill: z.boolean().optional().describe("Refill zones after placing (default true)"),
      limit: z.number().int().optional().describe("Max vias listed in the reply (default 100)"),
      dryRun: z
        .boolean()
        .optional()
        .describe("Report what would be placed without changing the board"),
    },
    async (args: any) => asText(await callKicadScript("stitch_ground", args)),
  );

  server.tool(
    "tidy_silkscreen",
    "Re-place (and optionally resize) footprint reference designators on the silkscreen so they clear pads/mask openings, other silk, other courtyards and the board edge. " +
      "Collisions use KiCad's stroke geometry (a connector's outline rectangle is four strokes, not a solid box). A reference already clear stays put (unless moveAll); " +
      "others try spots around the part (body centre for large parts), at 0 or 90 degrees; one that fits nowhere is hidden (hideIfNoFit) or reported unresolved. " +
      "Units mm. Returns {processed, collidingBefore, kept, moved, resized, hidden:[refs], unresolved:[refs], moves:[{ref,from,to}] (first `limit`), saved}.",
    {
      textHeight: z
        .number()
        .optional()
        .describe(
          "New text height in mm, e.g. 0.8 (JLCPCB minimum legible); omit to keep current sizes",
        ),
      textWidth: z.number().optional().describe("Text width in mm (default = textHeight)"),
      textThickness: z
        .number()
        .optional()
        .describe("Stroke thickness in mm (default 0.15 at 0.8 mm height, scaled)"),
      keepUpright: z.boolean().optional().describe("Keep text upright (default true)"),
      hideIfNoFit: z
        .boolean()
        .optional()
        .describe(
          "Hide references that fit nowhere (default true); false leaves them and lists them as unresolved",
        ),
      refs: z
        .array(z.string())
        .optional()
        .describe("Only these references (default: all visible silk references)"),
      side: z
        .enum(["front", "back", "both"])
        .optional()
        .describe("Board side(s) to process (default both)"),
      moveAll: z
        .boolean()
        .optional()
        .describe("Re-place references even when already clear (default false)"),
      includeHidden: z
        .boolean()
        .optional()
        .describe("Also try to place currently hidden references (default false)"),
      gap: z
        .number()
        .optional()
        .describe("Clearance from text to pads and other silk, mm (default 0.15)"),
      edgeClearance: z
        .number()
        .optional()
        .describe("Clearance from text to the board edge, mm (default 0.25)"),
      largePartArea: z
        .number()
        .optional()
        .describe("Courtyard area (mm^2) from which the body centre is tried first (default 25)"),
      courtyards: z
        .enum(["avoid", "ignore"])
        .optional()
        .describe("Keep text out of other parts' courtyards (default avoid)"),
      limit: z.number().int().optional().describe("Max moves listed in the reply (default 50)"),
      dryRun: z.boolean().optional().describe("Report the result without changing the board"),
    },
    async (args: any) => asText(await callKicadScript("tidy_silkscreen", args)),
  );

  server.tool(
    "add_pin_labels",
    "Add silkscreen text labels next to chosen pads of one footprint (e.g. connector pin names). For each pad it tries the requested side (auto = away from the part body, outside its courtyard), " +
      "horizontal text first at growing offsets, then rotated, then the other sides, and keeps the first spot that clears pads, silk, courtyards and the board edge (stroke geometry). " +
      "Labels go on the footprint's silk side as board text. Units mm. Returns {ref, layer, placed:[{pad,text,x,y,angle,side}], skipped:[{pad,text,reason}], saved}.",
    {
      ref: z.string().describe("Footprint reference, e.g. J3"),
      labels: z
        .record(z.string(), z.string())
        .describe('Pad number -> label text, e.g. {"1": "5V", "2": "GND"}'),
      side: z
        .enum(["auto", "left", "right", "top", "bottom"])
        .optional()
        .describe("Preferred side of each pad (default auto)"),
      textHeight: z.number().optional().describe("Text height in mm (default 1.0)"),
      textThickness: z
        .number()
        .optional()
        .describe("Stroke thickness in mm (default 0.15 x height)"),
      gap: z.number().optional().describe("Clearance to pads and other silk, mm (default 0.15)"),
      offset: z
        .number()
        .optional()
        .describe("Distance from the pad edge to the label, mm (default 0.3)"),
      edgeClearance: z
        .number()
        .optional()
        .describe("Clearance to the board edge, mm (default 0.25)"),
      avoidBody: z
        .boolean()
        .optional()
        .describe("Keep labels out of courtyards, the connector's own included (default true)"),
      layer: z
        .string()
        .optional()
        .describe("Silk layer override, F.SilkS or B.SilkS (default: the footprint's side)"),
      dryRun: z
        .boolean()
        .optional()
        .describe("Report the label positions without changing the board"),
    },
    async (args: any) => asText(await callKicadScript("add_pin_labels", args)),
  );

  server.tool(
    "cleanup_dangling",
    "Trim dangling track stubs back to their last junction (pad, via, track or zone contact) instead of deleting whole segments, repeating along a stub chain, " +
      "and remove vias that connect to nothing or only to one stub track (stitching vias inside a same-net zone are kept). " +
      "Dangling ends are the ones KiCad's DRC reports (track_dangling). The result is checked against KiCad connectivity: if any net has more separate pad groups " +
      "or the unconnected count rose, every change is rolled back (success=false, rolledBack=true). " +
      "Units mm. Returns {removedTracks, trimmedTracks, removedVias, unresolved, connectivity:{unconnectedBefore,unconnectedAfter,netsWorse}, actions (first `limit`), saved}.",
    {
      nets: z.array(z.string()).optional().describe("Only these nets (default: all)"),
      region: region.optional().describe("Only items with a point inside this rectangle (mm)"),
      removeVias: z.boolean().optional().describe("Also remove orphan/stub vias (default true)"),
      maxIterations: z
        .number()
        .int()
        .optional()
        .describe("Trim passes along stub chains (default 50)"),
      limit: z.number().int().optional().describe("Max actions listed in the reply (default 50)"),
      dryRun: z
        .boolean()
        .optional()
        .describe("Report what would change without changing the board"),
    },
    async (args: any) => asText(await callKicadScript("cleanup_dangling", args)),
  );

  server.tool(
    "transplant_layout",
    "Copy routing (tracks, arcs, vias, zones, rule areas) from a reference .kicad_pcb around a source anchor footprint onto the loaded board around a target anchor, " +
      "using the anchor-to-anchor transform (rotation + translation, mirrored when the anchors are on opposite sides; fitted to the anchor pads). " +
      "First verifies every source pad in the region: a target pad must be at the transformed position (+/- tolerance) with the mapped net. A source pad lying wholly on a " +
      "same-numbered target pad (an exposed pad split differently) is accepted and listed under offsetPads; a displaced pad is a padOffset mismatch. The net map is derived " +
      "from the pad correspondence (anchor pins outvote passives) unless netMap is given; two-pad parts with swapped nets are reported as reversedParts. " +
      "Mismatches on nets being copied abort unless force; mismatches on other nets are reported with blocking=false. `nets` restricts the copy (e.g. only the power nets). " +
      "Plated holes in source pads whose target pad has none (exposed-pad via arrays) become board vias (include pad_vias). Copied vias are grown to the board's minimum via size/drill (fitVias). " +
      "Items already present on the target are not duplicated. After copying it refills zones and reports pour-island splits, orphaned items (with the reason, e.g. it ended in a " +
      "reference pour that was not copied), rule areas that routers treat as obstacles, net-class clearance violations and shorts to existing copper " +
      "(shorts roll the copy back unless force; skipConflicting drops the offending items instead). " +
      "Units mm. Returns {transform:{rotationDeg,mirrored,...}, verification:{padsChecked,padsMatched,mismatchCount,blockingCount,mismatches,offsetPads,reversedParts}, " +
      "netMap:{count,derived,copied,renamed}, copied:{tracks,arcs,vias,zones,ruleAreas,padVias}, skipped:{unmappedNet,notSelected,crossingRegion,partialZones,alreadyPresent,...}, " +
      "report:{pourIslands,orphaned,ruleAreas,shorts,clearance,clearanceToMatchedPads,viasResized,droppedConflicting,connectivity}, saved}.",
    {
      sourceBoard: z.string().describe("Path to the reference .kicad_pcb"),
      sourceAnchorRef: z
        .string()
        .describe("Anchor footprint reference on the source board, e.g. U1"),
      targetAnchorRef: z
        .string()
        .describe("Matching anchor reference on the loaded board, e.g. U7"),
      netMap: z
        .record(z.string())
        .optional()
        .describe(
          "Source net -> target net; entries not given are derived from pad correspondence",
        ),
      region: region
        .optional()
        .describe("Area to copy (mm). Default: source anchor courtyard grown by regionMargin"),
      regionFrame: z
        .enum(["source", "target"])
        .optional()
        .describe("Which board's coordinates `region` is in (default source)"),
      regionMargin: z
        .number()
        .optional()
        .describe("Margin around the anchor courtyard for the default region, mm (default 3)"),
      include: z
        .array(z.enum(["tracks", "vias", "zones", "rule_areas", "pad_vias"]))
        .optional()
        .describe(
          "Item kinds to copy (default all). pad_vias: board vias where a source pad has a plated hole and the target pad has none",
        ),
      nets: z
        .array(z.string())
        .optional()
        .describe(
          "Copy only these nets (source or target names); default every mapped net. Pad mismatches on other nets do not block",
        ),
      verify: z.boolean().optional().describe("Abort on pad mismatches (default true)"),
      force: z
        .boolean()
        .optional()
        .describe("Copy despite blocking mismatches or shorts (default false)"),
      tolerance: z.number().optional().describe("Pad position tolerance in mm (default 0.05)"),
      replaceExisting: z
        .boolean()
        .optional()
        .describe(
          "First remove the target's tracks/vias, zones and rule areas of the copied nets lying wholly inside the region (vias in same-net pads are kept)",
        ),
      fitVias: z
        .boolean()
        .optional()
        .describe(
          "Grow copied vias to the board's minimum via diameter/drill/annular ring (default true)",
        ),
      skipConflicting: z
        .boolean()
        .optional()
        .describe(
          "Drop copied tracks/vias that short or violate net-class clearance against existing copper instead of rolling back (default false)",
        ),
      refill: z.boolean().optional().describe("Refill zones after copying (default true)"),
      fullNetMap: z
        .boolean()
        .optional()
        .describe("Include the full net map in the reply (default: only renamed nets)"),
      limit: z.number().int().optional().describe("Max entries per list in the reply (default 50)"),
      dryRun: z.boolean().optional().describe("Verify and report without changing the board"),
    },
    async (args: any) => asText(await callKicadScript("transplant_layout", args)),
  );
}
