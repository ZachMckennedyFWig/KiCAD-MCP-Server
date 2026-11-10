/**
 * Routing workflow tools (MCP_ISSUES R1, R3, R5): plane fan-out, a grid A*
 * single-connection router, bus routing on top of it, a length report and
 * serpentine length tuning. Python side: python/commands/fanout.py,
 * python/commands/astar_router.py and python/commands/length_tuning.py.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "./tool-response.js";

const endpoint = z
  .object({
    ref: z.string().optional().describe("Footprint reference, e.g. U7"),
    pad: z.string().optional().describe("Pad number/name on that footprint, e.g. 44"),
    x: z.number().optional().describe("X in mm (board coordinates)"),
    y: z.number().optional().describe("Y in mm (board coordinates)"),
    layer: z.string().optional().describe("Copper layer for an {x,y} endpoint, e.g. F.Cu"),
  })
  .describe("Either {ref, pad} or {x, y, layer?} (mm)");

const region = z
  .object({
    x1: z.number().optional(),
    y1: z.number().optional(),
    x2: z.number().optional(),
    y2: z.number().optional(),
    polygon: z
      .array(z.object({ x: z.number(), y: z.number() }))
      .optional()
      .describe("Polygon vertices in mm (instead of x1..y2)"),
    layers: z
      .array(z.string())
      .optional()
      .describe("Copper layers the region applies to (default: all)"),
  })
  .describe("Rectangle {x1,y1,x2,y2} or {polygon:[{x,y},...]} in mm");

const viaSpec = z
  .object({
    size: z.number().optional().describe("Via diameter in mm"),
    drill: z.number().optional().describe("Via drill in mm"),
  })
  .optional()
  .describe("Via size/drill in mm (default: the net's net-class via)");

const routeParams = {
  width: z.number().optional().describe("Track width in mm (default: the net class width)"),
  via: viaSpec,
  clearance: z
    .number()
    .optional()
    .describe(
      "Clearance to other-net copper in mm (default: the larger of the two nets' net-class clearances; .kicad_dru custom rules are not applied, run DRC after)",
    ),
  layersAllowed: z
    .array(z.string())
    .optional()
    .describe("Copper layers the route may use (default: the two outer layers)"),
  avoidRegions: z
    .array(region)
    .optional()
    .describe("Regions the route must not enter (tracks and vias)"),
  preferLayer: z.string().optional().describe("Layer to prefer; others cost otherLayerCost x"),
  otherLayerCost: z
    .number()
    .optional()
    .describe("Length multiplier on non-preferred layers (default 1.5)"),
  viaCost: z
    .number()
    .optional()
    .describe("Cost of a via as an equivalent track length in mm (default 2)"),
  gridStep: z
    .number()
    .optional()
    .describe("Routing grid in mm (default 0.1; refined to half automatically if no path)"),
  refine: z
    .boolean()
    .optional()
    .describe("Retry at half the grid step before searching the whole board (default true)"),
  searchMargin: z
    .number()
    .optional()
    .describe("Search window margin around the two ends in mm before the whole board (default 8)"),
  locked: z
    .boolean()
    .optional()
    .describe("Lock the written tracks/vias so autorouters keep them (default false)"),
  dryRun: z.boolean().optional().describe("Plan and check only; write nothing"),
};

export function registerRoutingWorkflowTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "fanout_to_planes",
    "Plane fan-out (R1): for every SMD pad on the given plane nets (e.g. GND, +3V3) that has no same-net via within servedRadius, place a short stub on the pad's layer and a via toward free space, so autorouters and zone fills connect it to the inner plane. Exposed (centre) pads get a via grid instead; fine-pitch pins are skipped. Every site is checked against all other-net copper (with the project's net-class clearances), same-net drills, the board edge, rule areas that forbid vias/tracks, optional courtyards, and the vias placed earlier in the run. regionPolicy 'zone' (default) only places a via where it lands in a same-net zone on another layer. All units mm. Returns {placed:{vias,stubs,pads}, skipped:{alreadyServed,finePitch,notInPlaneRegion,noRoom,notSmd}, noRoom:[\"REF.PAD (NET)\"], items:[{pad,net,kind:'stub'|'exposedPadGrid',via:{x,y},stub?:{from,to,width,layer}}]}. Nothing is written with dryRun; the board is unchanged on failure.",
    {
      nets: z.array(z.string()).describe('Plane nets to fan out, e.g. ["GND", "+3V3"]'),
      via: viaSpec,
      maxStub: z
        .number()
        .optional()
        .describe("Largest gap from the pad edge to the via centre in mm (default 1.6)"),
      servedRadius: z
        .number()
        .optional()
        .describe(
          "A same-net via this close to the pad centre (mm) already serves it (default 1.0)",
        ),
      skipFinePitch: z
        .union([z.boolean(), z.number()])
        .optional()
        .describe(
          "Skip pins whose nearest pad on the same footprint is closer than this pitch in mm (true = 0.65, false = never skip; default true)",
        ),
      exposedPadGrid: z
        .union([z.boolean(), z.object({ pitch: z.number().optional() })])
        .optional()
        .describe("Via grid in exposed pads: true (pitch 1.2 mm), {pitch}, or false"),
      regionPolicy: z
        .enum(["zone", "any"])
        .optional()
        .describe(
          "'zone' (default): only where the via lands in a same-net zone on another layer; 'any': anywhere",
        ),
      avoidCourtyards: z
        .union([z.boolean(), z.array(z.string())])
        .optional()
        .describe(
          "Keep vias out of other footprints' courtyards: true (all) or a list of references (default false)",
        ),
      largePadVias: z
        .number()
        .optional()
        .describe("Vias per large pad (area >= 2.5 mm^2, default 2)"),
      stubWidth: z.number().optional().describe("Stub width in mm (default: net class, <= pad)"),
      refs: z.array(z.string()).optional().describe("Only these footprints"),
      locked: z.boolean().optional().describe("Lock the new stubs/vias (default true)"),
      limit: z.number().optional().describe("Max items listed in the response (default 100)"),
      dryRun: z.boolean().optional().describe("Report what would be placed; write nothing"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("fanout_to_planes", args)),
  );

  server.tool(
    "route_connection",
    "Route one connection of a net with a grid A* router (R3): 45-degree moves on the allowed copper layers, layer changes through vias, obstacles = other-net pads/tracks/vias inflated by clearance + half width, rule areas that forbid tracks/vias, the board edge, and avoidRegions. Same-net copper is free, so the route may start/end anywhere on the copper already connected to each end. from/to are {ref,pad} or {x,y,layer}; omit both to route the closest connection KiCad reports as missing on the net. The path is re-checked with exact geometry before writing. Typical time: well under a second for an open 40 mm route, a few seconds when a fine-pitch pin needs the refined grid. Units mm. Returns {net, from, to, lengthMm, segments, vias, layersUsed, path:[{x,y,layer}], written, stats} or alreadyConnected:true. Nothing is written with dryRun; the board is unchanged on failure.",
    {
      net: z.string().describe("Net name"),
      from: endpoint.optional(),
      to: endpoint.optional(),
      ...routeParams,
    },
    async (args: any) => formatKicadResult(await callKicadScript("route_connection", args)),
  );

  server.tool(
    "route_bus",
    "Route several connections as a bus (e.g. an I2S/MCLK bundle). mode 'parallel' (default) routes a true parallel bundle: one guide corridor wide enough for every member is found with the A* router (on `layer`; layer changes only with allowVias and pitch >= via + clearance), each member's trunk is the guide offset by its lane x pitch with miter joins (octilinear, spacing = pitch measured perpendicular; outer traces are longer on bends), members are ordered across the bundle by their start pads (or `order`), and short A* fan-in/fan-out legs join the pads to the trunk ends. Everything is re-checked with exact clearance before an all-or-nothing write. If no corridor/legs/clearance work it falls back to mode 'sequential' and says why (modeUsed, fallbackReason). mode 'sequential' routes the connections one after another, later ones treating earlier ones as obstacles with mutual clearance pitch - width (no parallel geometry). Typical time: 1-5 s for 3-4 nets over 40 mm on a dense 4-layer board. Units mm. Returns {modeUsed, fallbackReason?, results:[per member: net, from, to, offsetMm, lengthMm, trunkLengthMm, legLengthsMm, segments, vias], lengths, lengthSpreadMm, segments, vias, order, pitch, layer, guide, parallelism:[{nets, trunkAtPitch, routeAtPitch}], notes, written}.",
    {
      connections: z
        .array(
          z.object({
            net: z.string(),
            from: endpoint.optional(),
            to: endpoint.optional(),
            width: z.number().optional(),
          }),
        )
        .describe(
          "Bus members (sequential mode: routing order). Omit from/to to route each net's closest missing connection",
        ),
      pitch: z
        .number()
        .optional()
        .describe(
          "Centre-to-centre spacing between bus members in mm (parallel default: width + clearance rounded up to 0.05; raised to that minimum if smaller)",
        ),
      mode: z
        .enum(["parallel", "sequential"])
        .optional()
        .describe("'parallel' (default): side-by-side bundle; 'sequential': one after another"),
      order: z
        .array(z.string())
        .optional()
        .describe(
          "Parallel: nets from left to right looking from the 'from' end along the bundle (default: by start pad position)",
        ),
      layer: z
        .string()
        .optional()
        .describe(
          "Parallel: layer of the bundle trunk (default: preferLayer or the first allowed layer)",
        ),
      allowVias: z
        .boolean()
        .optional()
        .describe(
          "Parallel: let the trunk change layers (needs pitch >= via size + clearance) and the legs use vias (default false)",
        ),
      breakoutRadius: z
        .number()
        .optional()
        .describe(
          "Parallel: fixed distance in mm from each end's pad centroid where the trunk may start/end (default: adaptive)",
        ),
      legRoom: z
        .number()
        .optional()
        .describe(
          "Parallel: extra room in mm kept between the pads and the trunk ends for the fan-in legs (default 0.8, + via size with allowVias)",
        ),
      timeBudget: z
        .number()
        .optional()
        .describe("Parallel: seconds to spend before falling back to sequential (default 60)"),
      ...routeParams,
    },
    async (args: any) => formatKicadResult(await callKicadScript("route_bus", args)),
  );

  server.tool(
    "length_match",
    "Report the routed length of nets and differential pairs against a target (R5). Length = sum of the net's track/arc lengths (vias and pad internals excluded). target defaults to the longest net in the set. Report only (read-only tool): use tune_length to add serpentine meanders. Units mm. Returns {targetMm, toleranceMm, allWithinTolerance, nets:[{net,lengthMm,deltaMm,withinTolerance,addMm,segments,vias,routed}], pairs:[{positive,negative,skewMm,withinTolerance,shorter}]}.",
    {
      nets: z.array(z.string()).optional().describe("Nets to compare"),
      pairs: z
        .array(z.array(z.string()))
        .optional()
        .describe("Differential pairs as [positiveNet, negativeNet]"),
      target: z.number().optional().describe("Target length in mm (default: longest)"),
      tolerance: z.number().optional().describe("Allowed deviation in mm (default 0.5)"),
      pairTolerance: z.number().optional().describe("Allowed pair skew in mm (default: tolerance)"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("length_match", args)),
  );

  server.tool(
    "tune_length",
    "Serpentine (meander) length tuning: replace part of straight track segments of a net with a meander that adds exactly the missing length (KiCad-style 45-degree chamfered corners by default). Target: targetLength (mm), or matchTo (a net, or a list of nets = their longest), or nets:[...] alone (tune all up to the longest). pairs:[[P,N]] tunes each pair's shorter member up to its partner with small, tight bumps. Segments are used longest first (or the given segments uuids / region); the missing length is spread over several segments if one is not enough, and the amplitude shrinks next to obstacles. Every meander is checked against other-net copper, pads, holes, keepouts, the board edge and the net's own copper (project net-class clearances) on both sides and at smaller amplitudes; segments that do not fit are skipped with a reason. Only adds length (a net longer than the target is reported, not shortened). All meanders are written in one transaction or none. Zone fills are not obstacles: refill zones and run DRC after. Units mm. Returns {allWithinTolerance, nets:[{net,targetMm,beforeMm,plannedMm,afterMm?,addedMm,remainingMm,status:'tuned'|'partial'|'notTuned'|'withinTolerance'|'tooLong',meanders,segments:[{uuid,layer,segmentLengthMm,meanders,addedMm,amplitudeMm,side}],skipped:[{uuid,reason}]}], segmentsReplaced, meanders, written}. Nothing is written with dryRun.",
    {
      net: z.string().optional().describe("Net to tune"),
      nets: z
        .array(z.string())
        .optional()
        .describe("Several nets to tune to the same target (default target: the longest of them)"),
      targetLength: z.number().optional().describe("Target routed length in mm"),
      matchTo: z
        .union([z.string(), z.array(z.string())])
        .optional()
        .describe("Match the length of this net, or of the longest of these nets"),
      pairs: z
        .array(z.array(z.string()))
        .optional()
        .describe("Differential pairs [positiveNet, negativeNet]: the shorter member is tuned"),
      tolerance: z
        .number()
        .optional()
        .describe("Nets this close to the target (mm) are left alone (default 0.1)"),
      pairTolerance: z.number().optional().describe("Allowed pair skew in mm (default: tolerance)"),
      amplitude: z
        .number()
        .optional()
        .describe(
          "Largest meander height from the track centre line in mm (default 1.0; pairs max(2 x width, 0.3))",
        ),
      spacing: z
        .number()
        .optional()
        .describe(
          "Meander pitch (centre-to-centre of adjacent legs) in mm (default max(3 x width, width + clearance); pairs max(2 x width, width + clearance))",
        ),
      style: z
        .enum(["trombone", "accordion", "rounded"])
        .optional()
        .describe(
          "trombone: single-sided bumps (default); accordion: alternating sides, more length per mm; rounded: trombone with arc corners",
        ),
      corner: z
        .enum(["chamfer", "round", "square"])
        .optional()
        .describe("Corner shape: 45-degree chamfer (default), tangent arc, or square"),
      segments: z
        .array(z.string())
        .optional()
        .describe("Only these track segments (KiCad UUIDs, e.g. from query_traces), in this order"),
      region: region
        .optional()
        .describe("Only place meanders whose base lies inside this rectangle/polygon (mm)"),
      layers: z.array(z.string()).optional().describe("Only tune segments on these copper layers"),
      clearance: z
        .number()
        .optional()
        .describe(
          "Clearance to other-net copper in mm (default: the net-class clearances; .kicad_dru custom rules are not applied, run DRC after)",
        ),
      allowLocked: z
        .boolean()
        .optional()
        .describe("Also tune locked segments (default false: they are skipped)"),
      locked: z.boolean().optional().describe("Lock the new meander pieces (default false)"),
      dryRun: z.boolean().optional().describe("Plan and check only; write nothing"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("tune_length", args)),
  );
}
