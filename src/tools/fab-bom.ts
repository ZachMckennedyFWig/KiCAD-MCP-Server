/**
 * BOM / sourcing, fabrication package, snapshots / undo and the pcbnew script
 * escape hatch (MCP_ISSUES S4, S5, F3, I1, I3).
 *
 * Python side: python/commands/{bom_tools,jlc_sourcing,fab_package,snapshots,
 * script_runner}.py.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "./tool-response.js";

const matchSchema = z
  .object({
    value: z
      .union([z.string(), z.array(z.string())])
      .optional()
      .describe("Exact Value field (or any of a list)"),
    valueRegex: z.string().optional().describe("Regex that must match the whole Value"),
    footprint_contains: z
      .string()
      .optional()
      .describe("Case-insensitive substring of the Footprint field, e.g. 'R_0603'"),
    ref_regex: z
      .string()
      .optional()
      .describe("Regex that must match a whole reference, e.g. 'J(1|2|7)'"),
    lib_id: z.string().optional().describe("Exact symbol lib id, e.g. 'Device:R'"),
  })
  .describe("All given conditions must hold (AND). At least one is required.");

const setSchema = z
  .record(z.string(), z.union([z.string(), z.boolean(), z.number()]))
  .describe(
    "Fields to set: any field name -> string (e.g. LCSC, MPN, Value, Footprint; 'LCSC' updates an " +
      "existing 'LCSC Part #'-style field in place), plus the flags dnp, in_bom, on_board, " +
      "exclude_from_sim, in_pos_files -> true/false.",
  );

const constraintsSchema = z
  .object({
    value: z
      .string()
      .optional()
      .describe("Part value in any notation: '10k', '4k7', '100nF', '4.7uH', '12MHz'"),
    tolerance: z.string().optional().describe("Maximum tolerance, e.g. '1%'"),
    voltage: z.string().optional().describe("Minimum voltage rating, e.g. '25V'"),
    package: z
      .string()
      .optional()
      .describe("JLC package name, exact ('0603', 'SOT-23-5') or a glob ('SOT-23*')"),
    subcategory: z
      .string()
      .optional()
      .describe("JLC subcategory, e.g. 'Chip Resistor - Surface Mount' (default: the lcsc part's)"),
    basicOnly: z.boolean().optional().describe("Only Basic parts"),
    noFeeOnly: z.boolean().optional().describe("Only Basic or Preferred parts (no loading fee)"),
    minStock: z.number().int().optional().describe("Minimum stock"),
    attributes: z
      .record(z.string(), z.string())
      .optional()
      .describe("Extra catalog attributes that must match, e.g. {'Temperature Coefficient':'X7R'}"),
  })
  .describe("Parametric filters; any given value/tolerance/voltage replaces the reference's");

export function registerFabBomTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "apply_property_map",
    `Bulk-set symbol fields and flags across the WHOLE schematic hierarchy from a list of rules (MCP_ISSUES S4; replaces lcsc_map-style scripts).
Each rule is {match: {value?, valueRegex?, footprint_contains?, ref_regex?, lib_id?}, set: {LCSC?: "C25804", <field>: "text", dnp?: bool, in_bom?: bool, on_board?: bool, exclude_from_sim?: bool}}.
Rules apply in order to every non-power symbol; all matching rules apply (later ones win) unless firstMatchOnly. Idempotent: values already set are not rewritten, so a second run changes nothing. All sheets are written atomically (all or none).
Returns {changedParts, changed:[{refs, sheet, changes:{field:[old,new]}}], rules:[{index, matched, refs, warning?}], unmatched:[{ref,value,footprint}] (no rule matched), missingLcsc:[...] (BOM parts, not DNP, still without an LCSC number), sheetsChanged}.`,
    {
      schematicPath: z
        .string()
        .describe(
          "Root .kicad_sch (any sheet of the project, a .kicad_pro or project dir also works)",
        ),
      rules: z
        .array(z.object({ match: matchSchema, set: setSchema, name: z.string().optional() }))
        .describe("Rules applied in order"),
      dryRun: z
        .boolean()
        .optional()
        .describe("Report what would change without writing (default false)"),
      firstMatchOnly: z
        .boolean()
        .optional()
        .describe(
          "Apply only the first matching rule per part, like a lookup table (default false)",
        ),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("apply_property_map", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "jlc_bom_report",
    `JLCPCB assembly cost/stock review of a BOM (MCP_ISSUES S5). Groups parts by LCSC number and, per line, reports basic / preferred / extended, stock vs qtyPerBoard x boards, unit price at that quantity, the extended-part loading fee, and fee-free (basic/preferred) alternatives in the same package whose parameters are compatible (value equal; tolerance, voltage, power, current, dielectric equal or better). ICs and other parts without a parametric class get no automatic suggestion. Reads the local JLC catalog read-only (run download_jlcpcb_database first).
Source: schematicPath (DNP / not-in-BOM parts skipped, 'exclude' refs left out as hand-soldered) or bom rows [{lcsc, refs?, qty?, value?, footprint?}].
Returns {totals:{lines, parts, boards, basicLines, preferredLines, extendedLines, loadingFee ($), partsCost ($), stockShort, notFound, potentialFeeSaving}, lines:[{lcsc, refs, qtyTotal, libraryType, fee, stock, stockOk, unitPrice, status, alternatives:[{lcsc, libraryType, mfr, stock, unitPrice, attributes}]}], missingLcsc}. detail=summary (default) lists only lines needing attention.`,
    {
      schematicPath: z.string().optional().describe("Root .kicad_sch of the design"),
      bom: z
        .array(
          z.object({
            lcsc: z.string().optional(),
            refs: z.union([z.string(), z.array(z.string())]).optional(),
            qty: z.number().int().optional(),
            value: z.string().optional(),
            footprint: z.string().optional(),
          }),
        )
        .optional()
        .describe("BOM rows instead of a schematic"),
      boards: z.number().int().optional().describe("Number of boards assembled (default 1)"),
      extendedFee: z
        .number()
        .optional()
        .describe("Loading fee per unique extended part in USD (default 3)"),
      alternatives: z
        .number()
        .int()
        .optional()
        .describe("Max suggested alternatives per line, 0 to skip (default 3)"),
      exclude: z.array(z.string()).optional().describe("Refs left out of assembly (hand-soldered)"),
      detail: z.enum(["summary", "full"]).optional().describe("summary (default) or full"),
      dbPath: z.string().optional().describe("Catalog file to use instead of the default"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("jlc_bom_report", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "jlc_find_alternative",
    `Parametric JLCPCB part search (MCP_ISSUES S5). With lcsc: finds drop-in replacements for that part (same subcategory and package, compatible parameters). With constraints only: searches a subcategory/package by value, tolerance, voltage, stock and library type. Fee-free parts rank first, then stock, then price. Read-only.
Returns {reference?, filters:{subcategory, package, library, parametricClass}, count, alternatives:[{lcsc, libraryType, mfr, package, stock, unitPrice, attributes, notes?}]}. notes say when no parametric class exists (check the datasheet).`,
    {
      lcsc: z.string().optional().describe("Reference part, e.g. 'C25804'"),
      constraints: constraintsSchema.optional(),
      limit: z.number().int().optional().describe("Max results (default 10, max 100)"),
      quantity: z
        .number()
        .int()
        .optional()
        .describe("Quantity for price break and stock (default 1)"),
      dbPath: z.string().optional().describe("Catalog file to use instead of the default"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("jlc_find_alternative", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "fab_package",
    `One-call JLCPCB fabrication package (MCP_ISSUES F3). Writes into outputDir: gerbers/ (all copper, mask, paste, silk, Edge.Cuts, job file, Excellon PTH/NPTH + Gerber X2 drill maps) and <board>_gerbers_JLCPCB.zip; <board>_BOM_JLCPCB.csv (Comment, Designator, Footprint, LCSC Part #); <board>_CPL_JLCPCB.csv (Designator, Mid X, Mid Y, Layer, Rotation, from kicad-cli pos, mm) with the JLC rotation-correction table applied (SOT-23 +180, QFN/SOIC/TSSOP +270, ...; configurable, every correction is listed); <board>_HAND_SOLDER_parts.csv (handSolder refs: not assembled, still listed with LCSC); <board>-pos.csv; <board>_fab_report.json.
Also runs the fee/stock report (jlc_bom_report), DRC (project rules, schematic parity, saved zone fills) and ERC, re-checks stored assert_connectivity sets and schematic/board parity. Reads the SAVED board and schematic; never modifies the design. Takes up to a few minutes on large boards.
Returns {readyToOrder, blockers[], warnings[], files{gerberZip, bom, cpl, handSolder, pos, report}, counts{bomLines, assembledParts, cplRows, handSolderLines, gerberFiles, drillFiles, missingLcsc}, rotationCorrections[{ref, package, kicad, jlc, rule}], fees{totals, lines}, checks{drc, erc, connectivity, parity}}.`,
    {
      fab: z.enum(["jlcpcb"]).optional().describe("Fabricator (only jlcpcb)"),
      boardPath: z.string().optional().describe(".kicad_pcb (default: from projectPath)"),
      projectPath: z.string().optional().describe(".kicad_pro or project directory"),
      schematicPath: z.string().optional().describe("Root .kicad_sch (default: the project's)"),
      outputDir: z.string().describe("Output directory (created if missing)"),
      options: z
        .object({
          handSolder: z
            .array(z.string())
            .optional()
            .describe("Refs to keep out of assembly but list in the hand-solder parts file"),
          excludeFromAssembly: z
            .array(z.string())
            .optional()
            .describe("Refs to leave out of BOM/CPL entirely"),
          rotationOverrides: z
            .record(z.string(), z.number())
            .optional()
            .describe("Per-ref rotation correction in degrees (replaces the table for that ref)"),
          rotationTable: z
            .union([
              z.enum(["default", "none"]),
              z.array(z.object({ pattern: z.string(), degrees: z.number() })),
            ])
            .optional()
            .describe("Footprint-name regex -> degrees; 'default' (built-in table) or 'none'"),
          zip: z.boolean().optional().describe("Zip the Gerber/drill files (default true)"),
          runChecks: z
            .boolean()
            .optional()
            .describe("Run DRC/ERC/parity/connectivity (default true)"),
          boards: z
            .number()
            .int()
            .optional()
            .describe("Board quantity for the stock/fee report (default 5)"),
          extendedFee: z.number().optional().describe("USD per unique extended part (default 3)"),
          alternatives: z
            .number()
            .int()
            .optional()
            .describe("Alternatives per fee line (default 2)"),
          dbPath: z.string().optional().describe("JLC catalog file to use"),
          useBoardPlotParams: z
            .boolean()
            .optional()
            .describe("Use the board's own Gerber plot settings instead of the JLC layer set"),
        })
        .optional(),
      timeout: z.number().optional().describe("Per kicad-cli step, seconds (default 600)"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("fab_package", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "list_snapshots",
    `List project snapshots in <project>/snapshots, newest first (MCP_ISSUES I1): manual ones from snapshot_project (kind 'legacy'), automatic ones taken before mutating tools when KICAD_MCP_AUTO_SNAPSHOT=1 (kind 'auto'; last KICAD_MCP_AUTO_SNAPSHOT_KEEP kept, default 20) and 'safety' ones taken by restore_snapshot. Read-only.
Returns {count, snapshots:[{id, label, kind, tool, created, files}], autoSnapshot:{enabled, keep}}.`,
    {
      projectPath: z
        .string()
        .optional()
        .describe("Project directory or any project file (default: the loaded board's)"),
      kind: z.enum(["auto", "safety", "legacy", "manual"]).optional().describe("Only this kind"),
      limit: z.number().int().optional().describe("Max entries (default 20)"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("list_snapshots", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "restore_snapshot",
    `Restore the design files (.kicad_pcb/.kicad_sch/.kicad_pro/...) held by a snapshot (MCP_ISSUES I1). Takes a 'safety' snapshot of the files it will overwrite first (restore that to undo), writes each file atomically, and reloads the in-memory board when the loaded board is restored. Files not in the snapshot are kept. Refuses when the loaded board has unsaved changes unless force=true.
Returns {files:[{path, action: overwrite|create|unchanged}], changedFiles, safetySnapshot, keptFilesNotInSnapshot, boardReloaded?}.`,
    {
      snapshot: z.string().describe("Snapshot id, label, unique id fragment, or 'latest'"),
      projectPath: z
        .string()
        .optional()
        .describe("Project directory or any project file (default: the loaded board's)"),
      dryRun: z.boolean().optional().describe("Only report what would be restored"),
      files: z
        .array(z.string())
        .optional()
        .describe("Restore only these files (paths relative to the project, or file names)"),
      force: z.boolean().optional().describe("Discard unsaved in-memory board changes"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("restore_snapshot", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "diff_snapshots",
    `Compare two snapshots, or a snapshot with the current files (MCP_ISSUES I1). Read-only.
Board: footprints added/removed/moved (dx, dy in mm, drot in degrees, layer) and value/footprint changes, track and via count deltas per net (with track length delta in mm), zones and rule areas added/removed/reshaped. Schematic: symbols added/removed and changed fields/flags by reference, label/wire/junction count deltas per sheet. Also lists files added/removed/modified.
Returns {a, b, files[], board:{footprints, tracks, vias, zones}, schematic:{symbols, sheets}}.`,
    {
      a: z.string().describe("Older snapshot (id, label or 'latest')"),
      b: z
        .string()
        .optional()
        .describe("Newer snapshot, or 'current' for the files on disk (default)"),
      projectPath: z
        .string()
        .optional()
        .describe("Project directory or any project file (default: the loaded board's)"),
      limit: z.number().int().optional().describe("Max items per list (default 50)"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("diff_snapshots", args);
      return formatKicadResult(result);
    },
  );

  server.tool(
    "run_pcbnew_script",
    `EXECUTES ARBITRARY PYTHON CODE with the server's permissions (MCP_ISSUES I3 escape hatch). The code runs in a separate KiCad-Python subprocess against a fresh load of the board from disk (not the server's in-memory board), with a hard timeout.
Predefined: board (pcbnew.BOARD), pcbnew, args (your 'args' object), mm(v)/to_mm(v) unit helpers, footprint(ref), fpid_of(fp) ('Lib:Name', safe when GetFPID is broken), load_footprint('Lib:Name') / place_footprint(spec, ref, x_mm, y_mm, rot, layer) (list specs in preloadFootprints so they load before the board: KiCad 10 SWIG trap), zone_layers(zone), add_zone(net, layer, [[x,y],...] mm, priority, clearance, min_width, name), refill(), check(obj, cls). Set 'result = ...' or 'return ...' at top level to return JSON.
save=true saves the board atomically only if the file did not change meanwhile, then the server reloads its in-memory board from it (refused while the loaded board has unsaved changes unless discardUnsaved=true). Not available in readonly/manufacturing modes.
Returns {success, returnValue, stdout, stderr, error?, traceback?, saved, durationSec}.`,
    {
      code: z.string().describe("Python source to run"),
      boardPath: z.string().optional().describe(".kicad_pcb to load (default: the loaded board)"),
      save: z
        .boolean()
        .optional()
        .describe("Save the board after the script succeeds (default false)"),
      timeout: z
        .number()
        .optional()
        .describe("Seconds before the script is killed (default 60, max 1800)"),
      args: z
        .record(z.string(), z.any())
        .optional()
        .describe("JSON object exposed to the script as 'args'"),
      preloadFootprints: z
        .array(z.string())
        .optional()
        .describe("'Lib:Name' footprints to load before the board (for load_footprint)"),
      discardUnsaved: z
        .boolean()
        .optional()
        .describe("Allow save even though the loaded board has unsaved in-memory changes"),
    },
    async (args: Record<string, unknown>) => {
      const result = await callKicadScript("run_pcbnew_script", args);
      return formatKicadResult(result);
    },
  );
}
