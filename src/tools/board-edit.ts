/**
 * Board editing primitives, zones/rule areas and rules-as-data
 * (MCP_ISSUES §2.5 E1-E3, §2.8 C2-C3, §2.9).
 *
 * All coordinates and lengths are mm, board coordinates (KiCad origin top-left,
 * Y down). Mutating tools validate everything first (a failed call leaves the
 * board unchanged), accept `dryRun`, save through the server's auto-save, and
 * with `drcDelta: true` return the DRC violations the edit added or resolved.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { logger } from "../logger.js";
import { formatKicadResult } from "./tool-response.js";

type CommandFunction = (command: string, params: Record<string, unknown>) => Promise<any>;

const point = z.array(z.number()).length(2).describe("[x, y] in mm");
const polygon = z
  .union([
    z.array(point).min(3),
    z.object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() }),
    z.literal("board"),
  ])
  .describe('Polygon [[x, y], ...] (mm), a box {x1, y1, x2, y2}, or "board" for the board outline');

/** Shared by every mutating tool here. */
const mutationParams = {
  dryRun: z
    .boolean()
    .optional()
    .describe("Validate and report the would-be change without touching the board"),
  drcDelta: z
    .boolean()
    .optional()
    .describe(
      "Save now and run DRC (project rules) to report violations this edit added/resolved (slower: one DRC, two when the last recorded run is stale)",
    ),
  drcLimit: z
    .number()
    .int()
    .min(0)
    .optional()
    .describe("Max new/resolved items listed (default 10)"),
  refillZones: z
    .boolean()
    .optional()
    .describe("Refill zones in the drcDelta DRC run (default true)"),
  projectPath: z
    .string()
    .optional()
    .describe(".kicad_pro whose rules the drcDelta DRC uses (default: the board's project)"),
};

const zoneOptions = {
  priority: z.number().int().optional().describe("Zone priority; higher fills first"),
  clearance: z.number().optional().describe("Zone clearance in mm"),
  minWidth: z.number().optional().describe("Minimum copper width in mm"),
  padConnection: z
    .enum(["thermal", "solid", "none", "thermal_tht"])
    .optional()
    .describe("How pads connect to the zone"),
  thermalGap: z.number().optional().describe("Thermal relief gap in mm"),
  thermalSpokeWidth: z.number().optional().describe("Thermal spoke width in mm"),
  islandRemoval: z.enum(["always", "never", "area"]).optional().describe("Island removal mode"),
  minIslandArea: z
    .number()
    .optional()
    .describe("Minimum island area in mm² (islandRemoval 'area')"),
  fillType: z.enum(["solid", "hatched"]).optional().describe("Fill style"),
};

const forbidList = z
  .array(z.enum(["tracks", "vias", "pads", "pours", "footprints"]))
  .min(1)
  .describe("What the rule area forbids");

export function registerBoardEditTools(server: McpServer, callKicadScript: CommandFunction) {
  // ---------------------------------------------------------------- E1
  server.tool(
    "add_copper",
    "Add tracks (polylines) and vias in one all-or-nothing batch (MCP_ISSUES E1). Items: " +
      "{track: {net, layer, width, points: [[x,y],...]}}, {via: {net, at: [x,y], size, drill, " +
      "layers?: [top, bottom]}}, or spec strings 'T net layer width x1 y1 x2 y2 ...' / " +
      "'V net x y [size drill]' (mm; layer F/B/In1 shorthands ok; width/size/drill default to the " +
      "board's current values). Every item is validated first; any error adds nothing. Returns " +
      "{success, message, added: {trackSegments, vias}, items, uuids, drc?}.",
    {
      items: z
        .array(
          z.union([
            z.string(),
            z.object({
              track: z
                .object({
                  net: z.string().optional(),
                  layer: z.string().optional(),
                  width: z.number().optional(),
                  points: z.array(point).min(2),
                  locked: z.boolean().optional(),
                })
                .optional(),
              via: z
                .object({
                  net: z.string().optional(),
                  at: point,
                  size: z.number().optional(),
                  drill: z.number().optional(),
                  layers: z.array(z.string()).length(2).optional(),
                  locked: z.boolean().optional(),
                })
                .optional(),
            }),
          ]),
        )
        .min(1)
        .describe("Tracks and vias to add"),
      locked: z
        .boolean()
        .optional()
        .describe("Lock the new copper (default false); lock hand routing before autorouting"),
      ...mutationParams,
    },
    async (args) => {
      logger.debug(`add_copper: ${args.items.length} item(s)`);
      return formatKicadResult(await callKicadScript("add_copper", args));
    },
  );

  server.tool(
    "remove_copper",
    "Remove tracks/arcs/vias matching a filter (MCP_ISSUES E1). Needs at least one of nets, " +
      "region or endpointNear. Locked copper is kept unless filter.locked is 'locked' or 'any' " +
      "(the result says how many were kept). Returns {success, message, removed: {count, byKind, " +
      "byNet}, items (first `limit`), skippedLocked?, drc?}.",
    {
      filter: z
        .object({
          nets: z.array(z.string()).optional().describe("Net names or globs ('/Audio/*')"),
          region: z
            .union([
              z.array(point).min(3),
              z.object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() }),
            ])
            .optional()
            .describe("Polygon or box (mm)"),
          regionMode: z
            .enum(["inside", "touch"])
            .optional()
            .describe("inside (default): wholly inside the region; touch: any part"),
          endpointNear: z
            .object({ at: point, radius: z.number().optional() })
            .optional()
            .describe("Items with an end (or via centre) within radius mm (default 0.05)"),
          kinds: z
            .array(z.enum(["track", "arc", "via"]))
            .optional()
            .describe("Default: all"),
          locked: z
            .union([z.enum(["unlocked", "locked", "any"]), z.boolean()])
            .optional()
            .describe("Default 'unlocked'"),
          layer: z.string().optional().describe("Only this copper layer (vias spanning it)"),
          layers: z.array(z.string()).optional().describe("Only these copper layers"),
        })
        .describe("What to remove"),
      limit: z.number().int().min(0).optional().describe("Max removed items listed (default 20)"),
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("remove_copper", args)),
  );

  server.tool(
    "unroute",
    "Remove all tracks and vias of the given nets (MCP_ISSUES E1). Freerouting imports its " +
      "routing locked, so pass includeLocked=true to rip that up. Returns {success, message, " +
      "removed: {count, byKind, byNet}, skippedLocked?, hint?, drc?}.",
    {
      nets: z.array(z.string()).min(1).describe("Net names or globs"),
      includeLocked: z.boolean().optional().describe("Also remove locked copper (default false)"),
      keepVias: z.boolean().optional().describe("Remove tracks only (default false)"),
      limit: z.number().int().min(0).optional().describe("Max removed items listed (default 20)"),
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("unroute", args)),
  );

  // ---------------------------------------------------------------- E2
  const moveSpec = z.object({
    ref: z.string().describe("Reference, e.g. 'C12'"),
    x: z.number().optional().describe("New X (mm)"),
    y: z.number().optional().describe("New Y (mm)"),
    dx: z.number().optional().describe("Relative move in X (mm), instead of x/y"),
    dy: z.number().optional().describe("Relative move in Y (mm)"),
    rotation: z.number().optional().describe("Absolute rotation (degrees)"),
    side: z.enum(["front", "back"]).optional().describe("Flip to this side"),
  });
  const collisionParams = {
    checkCollisions: z.boolean().optional().describe("Report collisions (default true)"),
    margin: z.number().optional().describe("Extra courtyard clearance in mm (default 0)"),
    rejectOnCollision: z
      .boolean()
      .optional()
      .describe(
        "Undo everything if any part gains an overlap, leaves the board, or enters a keepout",
      ),
    skipPlaneNets: z
      .boolean()
      .optional()
      .describe("Leave nets that have a zone out of airwire lengths (default true)"),
    maxPadsPerNet: z
      .number()
      .int()
      .optional()
      .describe("Skip larger nets in airwires (default 64)"),
  };

  server.tool(
    "move_components",
    "Move/rotate/flip several footprints with collision feedback (MCP_ISSUES E2). For each part " +
      "returns position, overlaps and newOverlaps (courtyard polygons, same side), outsideBoard, " +
      "keepouts (rule areas forbidding footprints/pads), and airwireMm {before, after} (sum of " +
      "distances from its pads to the nearest same-net pad). dryRun measures at the proposed spots " +
      "and restores. Returns {success, message, parts: [...], collisionsAdded, drc?}.",
    {
      moves: z.array(moveSpec).min(1).describe("Moves to apply together"),
      ...collisionParams,
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("move_components", args)),
  );

  server.tool(
    "place_relative",
    "Place a part relative to another part or one of its pads, e.g. a decoupling cap next to an " +
      "IC supply pin (MCP_ISSUES E2). Target = anchor origin (or anchorPad centre) + offset; with " +
      "pad, that pad of `ref` lands on the target instead of its origin. alignToAnchor turns the " +
      "offset and rotation into the anchor's frame. Same collision feedback as move_components. " +
      "Returns {success, message, target, anchor, parts, drc?}.",
    {
      ref: z.string().describe("Part to place"),
      anchorRef: z.string().describe("Reference part"),
      anchorPad: z.string().optional().describe("Pad number on the anchor to measure from"),
      offset: point.optional().describe("[dx, dy] mm from the anchor point (default [0, 0])"),
      rotation: z
        .number()
        .optional()
        .describe("Rotation in degrees (absolute, or relative with alignToAnchor)"),
      alignToAnchor: z
        .boolean()
        .optional()
        .describe("Offset/rotation in the anchor's rotated frame"),
      pad: z.string().optional().describe("Pad of `ref` to put on the target"),
      side: z.enum(["front", "back"]).optional().describe("Flip to this side"),
      ...collisionParams,
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("place_relative", args)),
  );

  // ---------------------------------------------------------------- E3
  server.tool(
    "add_rule_area",
    "Add a rule area (keepout) on copper layers forbidding tracks, vias, pads, pours and/or " +
      "footprints (MCP_ISSUES E3). Layers accept 'F.Cu', 'In1', or '*.Cu' for all. Returns " +
      "{success, message, ruleArea: {uuid, name, layers, forbid, areaMm2}, drc?}.",
    {
      layers: z.array(z.string()).min(1).describe("Copper layers"),
      polygon,
      forbid: forbidList,
      name: z.string().optional().describe("Rule area name (usable in DRC rules and edit_zone)"),
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("add_rule_area", args)),
  );

  server.tool(
    "edit_zone",
    "Change an existing zone or rule area found by zone (uuid), zoneName, or net (+ layer); " +
      "several matches need all=true (the error lists them). Accepts add_zone's options plus " +
      "newNet, layers, polygon, rename, and forbid for rule areas. Fills are cleared when copper " +
      "geometry changes; run refill_zones. Returns {success, message, zones (after), before, drc?}.",
    {
      zone: z.string().optional().describe("Zone UUID (from query_zones)"),
      zoneName: z.string().optional().describe("Zone name"),
      net: z.string().optional().describe("Select zones of this net"),
      layer: z.string().optional().describe("Narrow the selection to a layer"),
      all: z.boolean().optional().describe("Edit every matching zone"),
      newNet: z.string().optional().describe("Assign this net"),
      layers: z.array(z.string()).optional().describe("New copper layers"),
      polygon: polygon.optional(),
      rename: z.string().optional().describe("New zone name"),
      forbid: forbidList.optional().describe("Rule areas: replace what is forbidden"),
      ...zoneOptions,
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("edit_zone", args)),
  );

  server.tool(
    "set_pad_zone_connection",
    "Set per-pad zone connection overrides: solid, thermal, none, thermal_tht or inherit (back to " +
      "the zone's setting). Select with pads ['U1:5'] and/or refs ['U1'] (every pad), narrowed by " +
      "nets globs. Returns {success, message, pads: [{ref, pad, net, from, to}], drc?}.",
    {
      pads: z.array(z.string()).optional().describe("'REF:PAD' entries, e.g. 'U7:57'"),
      refs: z.array(z.string()).optional().describe("Every pad of these parts"),
      nets: z.array(z.string()).optional().describe("Only pads on these nets (globs)"),
      connection: z.enum(["solid", "thermal", "none", "thermal_tht", "inherit"]),
      thermalSpokeWidth: z.number().optional().describe("Local spoke width override (mm)"),
      thermalGap: z.number().optional().describe("Local thermal gap override (mm)"),
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("set_pad_zone_connection", args)),
  );

  server.tool(
    "split_plane",
    "Build a multi-net plane on one layer in one call (MCP_ISSUES E3). regions is {net: polygon} " +
      "or [{net, polygon, priority?}]; earlier regions get higher priority (from basePriority, " +
      "default 1) so overlaps resolve in list order. replaceExisting removes the layer's current " +
      "copper zones first. Returns {success, message, layer, zones, removed, warnings, drc?}.",
    {
      layer: z.string().describe("Copper layer, e.g. 'In2.Cu'"),
      regions: z
        .union([
          z.record(z.string(), polygon),
          z.array(z.object({ net: z.string(), polygon, priority: z.number().int().optional() })),
        ])
        .describe("Net -> polygon"),
      basePriority: z.number().int().optional().describe("Priority of the last region (default 1)"),
      replaceExisting: z
        .boolean()
        .optional()
        .describe("Remove the copper zones already on this layer (default false)"),
      namePrefix: z.string().optional().describe("Name zones '<prefix><net>'"),
      layerType: z
        .enum(["power", "signal", "mixed", "jumper"])
        .optional()
        .describe("Also set the layer type (power for planes)"),
      ...zoneOptions,
      ...mutationParams,
    },
    async (args) => formatKicadResult(await callKicadScript("split_plane", args)),
  );

  // ---------------------------------------------------------------- C2
  server.tool(
    "add_drc_rule",
    "Append KiCad custom DRC rule(s) to the project's .kicad_dru with feedback (MCP_ISSUES C2). " +
      "Checks syntax (including value forms KiCad 10 silently rejects: unitless lengths, " +
      "'(min 2)' for min_resolved_spokes), then runs kicad-cli DRC on temporary copies before and " +
      "after the change with a canary rule, so a rules file KiCad drops is caught and nothing is " +
      "written. Reports how many items the rule's condition matches (assertion probe; capped at " +
      "199), violations citing it, and new/resolved violations. Returns {success, message, " +
      "kicadAccepted, matched, drc: {before, after, citingRule, newCount, resolvedCount, new, " +
      "resolved}, written, notes, warnings}.",
    {
      ruleText: z
        .string()
        .describe(
          `One or more rules, e.g. (rule "x" (condition "A.NetName == 'GND'") (constraint clearance (min 0.2mm)))`,
        ),
      replace: z.boolean().optional().describe("Replace existing rules with the same names"),
      dryRun: z.boolean().optional().describe("Check and measure, but don't write"),
      measure: z
        .boolean()
        .optional()
        .describe("Run the kicad-cli checks (default true; false = syntax check only)"),
      refillZones: z
        .boolean()
        .optional()
        .describe("Refill zones in the measuring DRC (default true)"),
      limit: z
        .number()
        .int()
        .min(0)
        .optional()
        .describe("Max new/resolved items listed (default 10)"),
      timeoutSec: z.number().int().optional().describe("kicad-cli timeout per run (default 300)"),
      projectPath: z.string().optional().describe(".kicad_pro whose .kicad_dru to edit"),
    },
    async (args) => formatKicadResult(await callKicadScript("add_drc_rule", args)),
  );

  server.tool(
    "list_drc_rules",
    "List the custom rules in the project's .kicad_dru: name, condition, layer, severity, " +
      "constraints. errors lists problems that make KiCad ignore the whole file (fileRejected). " +
      "Returns {success, message, druFile, exists, fileRejected, rules, errors, warnings}.",
    {
      includeText: z.boolean().optional().describe("Include each rule's source text"),
      projectPath: z.string().optional().describe(".kicad_pro whose rules to read"),
    },
    async (args) => formatKicadResult(await callKicadScript("list_drc_rules", args)),
  );

  server.tool(
    "remove_drc_rule",
    "Remove custom rule(s) by name from the project's .kicad_dru (atomic write; comments directly " +
      "above a rule go with it). Returns {success, message, removed, remaining, dryRun}.",
    {
      name: z.string().optional().describe("Rule name"),
      names: z.array(z.string()).optional().describe("Several rule names"),
      dryRun: z.boolean().optional().describe("Report without writing"),
      projectPath: z.string().optional().describe(".kicad_pro whose rules to edit"),
    },
    async (args) => formatKicadResult(await callKicadScript("remove_drc_rule", args)),
  );

  // ---------------------------------------------------------------- 2.9 / C3
  server.tool(
    "check_standards",
    "Check the board against the project's standards profile (standards.yaml/.yml/.json next to " +
      "the project; MCP_ISSUES §2.9): fab minimums (track, drill, annular ring, silkscreen, board " +
      "rules looser than the fab), region intrusion (allowed_nets/forbidden_nets, forbid_regions), " +
      "plane-split crossings for no_plane_split_crossing classes (from the zones on the plane " +
      "layers), class width and preferred layer, GND SMD pads without a nearby via, exposed pads " +
      "without vias, decoupling distance. Compact like run_drc: counts, first `limit` items, and a " +
      "delta against the previous check. run_drc appends the same summary when a profile exists.",
    {
      profilePath: z
        .string()
        .optional()
        .describe("Profile file (default: found next to the project)"),
      profile: z
        .record(z.string(), z.any())
        .optional()
        .describe("Inline profile object (same shape as the file)"),
      projectPath: z.string().optional().describe("Project directory or .kicad_pro to look in"),
      checks: z
        .array(z.string())
        .optional()
        .describe(
          "Run only these: fab, regions, netclasses, or a checks: key such as decoupling_within_mm",
        ),
      record: z
        .boolean()
        .optional()
        .describe("Record this run as the baseline for the next delta (default true)"),
      detail: z.enum(["summary", "items", "full"]).optional().describe("Default summary"),
      limit: z.number().int().min(0).optional().describe("Max items listed (default 20)"),
      ignore: z.array(z.string()).optional().describe("Violation-type globs to drop"),
      nets: z.array(z.string()).optional().describe("Only violations naming these nets"),
      refs: z.array(z.string()).optional().describe("Only violations naming these references"),
      region: z
        .object({ x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number() })
        .optional()
        .describe("Only violations inside this box (mm)"),
      severity: z.enum(["error", "warning", "info", "all"]).optional(),
    },
    async (args) => formatKicadResult(await callKicadScript("check_standards", args)),
  );
}
