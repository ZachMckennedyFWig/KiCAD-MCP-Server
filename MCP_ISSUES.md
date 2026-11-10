# KiCad MCP — bugs, quirks and wanted features

Part 1 (sections 1–7): bugs and quirks, each with repro, workaround and suggested fix.
Part 2 (section 2.x, at the end): wanted features, design principles and priorities for the MCP server.

Collected while building `CD_Mainboard` (KiCad 10.0, Windows 10, SWIG backend, MCP server started from VS Code).
Each entry: what happened, how to reproduce, workaround used, suggested fix.

---

## 1. `download_jlcpcb_database` fails with `WinError 32` (file locked)
- **Repro:** call `get_jlcpcb_database_stats` (DB empty), then `download_jlcpcb_database` (with or without `force`).
- **Result:** `[WinError 32] The process cannot access the file because it is being used by another process: ...\jlcpcb_parts.db`.
  The ~400 MB download itself completed into `~/.kicad-mcp/data/jlcparts_cache/cdfer.sqlite3`; only the final
  replace/swap of `jlcpcb_parts.db` failed. Several stale MCP server processes (python.exe from earlier sessions)
  were also still running and may hold the handle.
- **Workaround:** queried `cdfer.sqlite3` directly, read-only, with Python `sqlite3`.
- **Suggested fix:** close the DB connection opened by the stats/search tools (or use a context manager / `mode=ro`);
  write the new DB to a temp file and `os.replace()` after closing all handles; retry with backoff on Windows;
  surface the cache path in the error so users can recover.

## 2. Schematic reads use the locale encoding (cp1252) → `UnicodeDecodeError`
- **Repro:** use a project symbol library that contains non-ASCII text (e.g. easyeda2kicad output with
  `"Manufacturer" "XKB Connection(中国星坤)"`) in `batch_add_and_connect`.
- **Result:** the first call fails; the sheet now has the symbol embedded in `lib_symbols`, and every later call on
  that sheet fails with `UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d`.
- **Workaround:** stripped non-ASCII characters from the library and the sheet.
- **Suggested fix:** always `open(..., encoding="utf-8")` for KiCad files (they are UTF-8 by spec).

## 3. `batch_add_and_connect` reports failure without per-item reasons
- **Repro:** same as #2, or use a symbol the server can't resolve.
- **Result:** `Failed: Placed 0 component(s) (13 failed), connected 0 pin(s)` with no error text.
  `batch_add_components` given the same input does report the reason per component.
- **Suggested fix:** propagate the per-component error messages, as `batch_add_components` does.

## 4. Timeout truncates the schematic to 0 bytes (data loss)
- **Repro:** `batch_add_and_connect` with ~17 components (mixed `Device:*`, `Power_Management:*` and project-library
  symbols) on a sheet that already holds ~25 components.
- **Result:** `Command timeout after 30s`, and the target `.kicad_sch` was left at **0 bytes**; all work on that
  sheet was lost.
- **Workaround:** batches of ≤8 components and file backups between calls.
- **Suggested fix:** atomic writes (write to temp file in the same dir + `os.replace`); a longer or configurable
  timeout for batch tools; make the timeout cancel cleanly instead of killing a write mid-way.

## 5. Some stock KiCad 10 symbols are "not found in library"
- **Repro:** `batch_add_components` with `MCU_RaspberryPi:RP2350B` or `Power_Management:TPS22810DBV`.
- **Result:** `Symbol 'RP2350B' not found in library 'MCU_RaspberryPi'`. Both symbols exist in the installed
  KiCad 10 libraries (verified with grep), and neighbouring symbols in the same files
  (`Power_Management:TPS22917DBV`) load fine. `batch_list_symbol_pins` likewise fails for
  `Memory_RAM:APS6404L-3SQRx-SN` (a derived `extends` symbol).
- **Workaround:** copied the symbol blocks verbatim into a project library (`lib/CDMB_K.kicad_sym`); the MCP then
  loads them without problems.
- **Suggested fix:** check the library index/parser against these symbols — possibly a stale cache, a
  file-position or size limit, or `extends` resolution.

### 5b. Multi-level `extends` symbols corrupt the sheet
- **Repro:** place `Memory_RAM:APS6404L-3SQRx-SN` (extends `APS6404L-SQRx-SN`, which extends
  `APS1604M-SQRx-SN`), or copies of those in a project library.
- **Result:** the derived symbols are embedded in the sheet's `lib_symbols` *flat* (no `_1_1` sub-units). The next
  call on that sheet fails with `embedded flat lib symbols [...] have no sub-units and break the kicad-skip parser`.
  Note that `Memory_RAM:W25Q128JVS` (one level of `extends`) worked fine.
- **Workaround:** removed the broken `lib_symbols` entries by hand; copied the root symbol `APS1604M-SQRx-SN`
  into the project library under a new name and used that.
- **Suggested fix:** resolve the whole `extends` chain when embedding (merge base graphics and pins into
  `NAME_0_1` / `NAME_1_1` sub-units, as KiCad does when it saves).

## 5c. "Failed" placements still write component instances
- **Repro:** the failing calls in #5b. Each reported `Placed 0 component(s) (1 failed)`, but each call still wrote a
  `(symbol (lib_id ...))` instance for U9 into the sheet: three orphan copies of U9 at the same location.
- **Workaround:** deleted the orphan instance blocks by hand.
- **Suggested fix:** validate the library symbol before writing anything, and roll back the file on any error.

## 5d. USB-C symbol shield pin name
- Not a bug, a usability note: `Connector:USB_C_Receptacle_USB2.0_16P` names its shield pin `S1`, not `SH`.
  The error message did list the available pins, which made this easy to fix.

## 5e. ⚠ Embedded symbols differ from the library — wrong pin positions and renamed pins (SERIOUS)
- The MCP re-serialises every symbol it embeds in `lib_symbols`, and the copy is not faithful:
  - **Pin geometry changed:** for `Power_Management:TPS22917DBV`, `get_schematic_pin_locations` reported pin 5
    (QOD) at the same Y as pin 4 (`y = 165.1`). The real library puts it at `y = 167.64`. Labels placed by
    `batch_connect` therefore landed in the wrong spot. ERC then reported QOD shorted to VOUT (pins 5 and 6),
    and after re-syncing to the true library, pin 5 was unconnected.
  - **Pin numbers renamed:** `Connector:USB_C_Receptacle_USB2.0_16P` shield pin `SH` became `S1`.
  - **Format drift:** `Datasheet ""` → `"~"`, pin names `""` → `"~"`, dropped `show_name` / `do_not_autoplace`
    / `in_pos_files` / `embedded_fonts`, `(pin_numbers (hide yes))` → `(pin_numbers hide)`. KiCad 10 ERC flagged
    **every** placed symbol (184 warnings) as `lib_symbol_mismatch`.
- `update_symbol_from_library` reported "22 symbols updated" but the 184 warnings remained.
- **Risk:** a label placed on a mis-positioned pin can silently land on a *different* pin of the real symbol and
  connect the wrong net. ERC will not catch that.
- **Workaround:** replaced every embedded symbol with the verbatim library text (flattening `extends` chains);
  fixed the two QOD labels by hand; upgraded the EasyEDA-generated library to the KiCad 10 format; then checked the
  exported netlist pin by pin against the intended design (474 pins, 0 errors).
- **Suggested fix:** embed library symbols verbatim (only rename the top-level symbol to `Lib:Name`); compute pin
  positions from the same text KiCad will load; add a regression test comparing
  `get_schematic_pin_locations` with `kicad-cli` netlist output.

## 5f. `batch_connect(replace=true)` does not remove global labels
- After converting labels to global labels, `replace=true` placed the new local label but left the old
  `global_label` on the pin, which merged two nets (`multiple_net_names` warning).
- **Suggested fix:** `replace` should remove every label type on the pin (local, global, hierarchical).

## 5g. Deleting a component leaves its attached wires behind
- `delete_schematic_component(deleteAttachedLabels=true)` on a `PWR_FLAG` that had been joined by an auto-drawn wire
  removed the flag but left the wire dangling (`unconnected_wire_endpoint`).

## 5h. `sync_schematic_to_board` writes the board, then refuses to save its own change
- Each call reported success, and the footprints did land in the `.kicad_pcb` on disk. But the response also said
  `Auto-save refused: the on-disk PCB file's contents changed externally since this MCP session loaded it`
  (`memChangesUnsaved: true`). The "external" change was the tool's own write. The in-memory board is then stale
  until `open_board` is called again.
- It also does not update footprints that changed in the schematic (e.g. a new `Footprint` field on L2). Only
  missing footprints are added; nothing is reported about the mismatch.

## 5i. `replace_component` is listed but not implemented
- Calling it returns `{"success":false,"message":"Unknown command: replace_component"}`.
- **Workaround:** wrote `tools/swap_footprint.py` to swap a footprint in the board file, keeping position and nets.

## 5j. `add_zone` is listed but not implemented
- Returns `Unknown command: add_zone`. `add_copper_pour` works instead (but it only takes the board outline or a
  polygon, with no pad-connection or priority options).

## 5k. `create_netclass` doesn't persist the net assignments
- The response says `persisted: true`, and the class rules (width, clearance) were written to `.kicad_pro`. But the
  `nets` list was not written (`netclass_patterns` stayed empty), and the in-memory update failed with
  `'NETINFO_ITEM' object has no attribute 'SetClass'` (SWIG API change in KiCad 10).
- **Workaround:** wrote `netclass_patterns` into `.kicad_pro` directly.

## 5l. `export_dsn` fails with no reason
- `ExportSpecctraDSN returned: False`. The same call from KiCad 10's bundled Python
  (`pcbnew.ExportSpecctraDSN(pcbnew.LoadBoard(path), out)`) on the same saved board returns `True` and writes a
  valid DSN. The MCP's in-memory board may be in a state the exporter rejects. The error should be surfaced, or the
  tool should fall back to loading from disk.

## 5m. Long-running tools vs the 30 s command timeout
- Given #4 (a timeout truncated a file), I ran Freerouting from the shell rather than through `autoroute`, which can
  take minutes on a 190-net board. The timeout should be per-tool, and long jobs should run as background tasks.

## 5n. Workflow note: the board in memory goes stale relative to disk
- Several tools write the `.kicad_pcb` through a different path than the in-memory SWIG board (sync, DSN/SES
  round-trips), and others refuse to save afterwards. Running `open_board` after every write was the only reliable
  pattern. One source of truth (always reload from disk, or always write from memory) would remove a whole class of
  "changed externally" refusals.

## 5o. Two-level `extends` symbols still "not found" (again)
- **Repro:** `batch_add_and_connect` with `Interface_UART:THVD1450D`. It extends `MAX481E`, which extends `LTC2850xS8`.
- **Result:** `Symbol 'THVD1450D' not found in library 'Interface_UART'`. Same root cause as #5 / #5b.
- **Workaround:** copied the root symbol `LTC2850xS8` into the project library as `SN65HVD72DR`. The pinout is the
  standard RO, /RE, DE, DI, GND, A, B, VCC.

## 5p. Labels land 1.27 mm short of the pin on a rotated symbol
- **Repro:** `batch_add_and_connect` with `Jumper:SolderJumper_2_Bridged`, `rotation: 90`, at (154.94, 86.36), and
  nets `{1: X, 2: Y}`.
- **Result:** the labels were placed at y = 90.17 / 82.55. The real pin ends are at y = 91.44 / 81.28, so both pins
  were unconnected (ERC `pin_not_connected` + `label_dangling`). The MCP's pin geometry for this symbol differs from the
  library: it reports pin length 2.54 at ±3.81, while the library has pins at ±5.08.
- **Workaround:** moved the two labels by hand.
- **Suggested fix:** same as #5e. Compute pin positions from the verbatim library symbol, including rotation.

## 5q. `create_netclass` accepts net names that don't exist
- The net list given to `create_netclass` used `+5V` / `+12V`. On the board those nets are hierarchical
  (`/Power/+5V`), because the label is local to the Power sheet. The pattern silently matched nothing, so the
  1.0 mm class was never applied.
- **Suggested fix:** check each pattern against the board's net names and warn on zero matches (or offer the closest
  hierarchical name).

## 5r. KiCad 10 SWIG pitfalls that the MCP backend will also hit
(These are found from KiCad's bundled Python. They matter here because the MCP uses the same SWIG layer.)
- `pcbnew.FootprintLoad()` → `'SwigPyObject' object has no attribute 'FootprintLoad'`.
  `PCB_IO_KICAD_SEXPR().FootprintLoad()` works, but only **before** `LoadBoard()`. After a board is loaded, it returns
  bare `SwigPyObject`s with no `FOOTPRINT` methods.
- `FOOTPRINT.GetFPID()` returns an unusable `SwigPyObject`, so `LIB_ID` methods are not reachable. I read the
  footprint lib-id from the file text instead.
- `ZONE.GetLayerName()` on a freshly created zone reports `F.Cu`, even though the zone saves on the right layer.
- `ZONE.Outline().NewOutline()` sometimes returns a bare `SwigPyObject` (it worked on the first run and failed on the
  second run in the same script).
- `NETINFO_ITEM.SetClass` no longer exists (see #5k).
- **Suggested fix for the MCP:** for anything that loads footprints or edits zones, prefer s-expression edits or
  `kicad-cli` over SWIG; add a type check (`isinstance(x, pcbnew.FOOTPRINT)`) with a clear error.

## 6. Noisy "Power nets without PWR_FLAG" warnings
- Every `batch_*` call warns about plain signal nets (`DRV_PWR_EN`, `VIN_SENSE`, `EXT_PWR_FLT_N`, …) and about
  nets that already have a `PWR_FLAG` added in an earlier call or on another sheet.
- **Suggested fix:** only consider nets that contain a `power_in` pin and no `power_out` pin, and evaluate the
  whole sheet or hierarchy rather than just the current call.

## 7. Cosmetic: auto-wiring draws diagonal wires; labels collide on dense symbols
- When a "facing label for the same net is nearby", `batch_connect` / `batch_add_and_connect` draws a straight
  diagonal wire between the pins, which can cross other symbols.
- Labels placed on adjacent pins of passives (e.g. vertical R/C pairs) overlap reference/value text.
- **Suggested fix:** route with orthogonal segments (or just place a label) and offset labels from fields.

---

# Part 2 — Wanted features: a roadmap for the KiCad MCP server

Part 1 lists what broke. This part lists what was missing. Every item below is something I had to build by hand
(scripts in `CD_Mainboard/tools/`) or work around at a high token cost while taking this board from schematic to
JLCPCB-ready files. Each feature has:
- **Why**: the problem it solves, with evidence from this project;
- **What**: the tool or behaviour;
- **How**: an implementation sketch, plus the existing script that can serve as a reference implementation.

## 2.0 Where the tokens actually went

Roughly in order of cost:
1. **Re-reading and re-rendering the board to understand state.** I made many whole-board renders, zoomed copies and
   pad dumps just to answer questions like "what copper is near (83, 75)?".
2. **Recovering from silent corruption.** Wrong pin geometry (#5e/#5p), truncated files (#4), orphan instances
   (#5c) and stale in-memory boards (#5h/#5n) each cost a detect → diagnose → repair loop.
3. **Autorouter babysitting.** I ran 12 Freerouting passes of 3–12 minutes each. Most failures were caused by
   board state that Freerouting can't interpret: pour-only keepouts treated as obstacles, plane nets looking
   "unrouted", fixed-item violations. The tool never said so directly.
4. **Verifying results.** ERC can't catch a label landing on the wrong pin, so I wrote a pin-by-pin netlist
   checker. DRC on copied boards silently used default rules.
5. **Large default outputs.** Full DRC JSON, full label lists and full component lists were returned when only a
   count or the first few problems were needed.

The features below target those five costs directly.

## 2.1 Design principles for the tool set

These apply to every tool. They matter more than any single feature.

1. **One source of truth, atomic writes.**
   - Every mutating tool loads from disk, applies the change, writes to `file.tmp` and then calls `os.replace`.
   - The tool returns the file's new content hash.
   - No hidden in-memory board that drifts from disk (#5h, #5n). No partially written files (#4).
   - A mutation that fails leaves the file byte-identical (#5c).
2. **Verbatim library fidelity.** Symbols and footprints are copied from the library text, never re-serialised.
   Pin and pad positions are computed from that same text. This alone would have prevented #5e and #5p, the most
   dangerous class of bug (a wrong connection that ERC cannot see).
3. **Summaries first, detail on request.**
   - Every query and check tool takes `detail: "summary" | "items" | "full"`, `limit`, and filters (`nets`,
     `refs`, `layer`, `region`).
   - The default response is counts plus the top N problems.
   - This is the single biggest token saver.
4. **Deltas, not snapshots.**
   - After a mutation, return what changed: added/removed items, plus new or resolved DRC/ERC/connectivity
     problems.
   - The caller should never have to re-run a full check to learn whether an edit helped.
5. **Transactions with dry-run.**
   - Batch tools validate every item first and report per-item errors (#3).
   - They then commit all items or none.
   - `dry_run: true` returns the would-be delta without writing anything.
6. **Asynchronous long jobs.**
   - Routing, DRC on big boards, database downloads and exports return a `job_id`.
   - The caller then uses `job_status` (progress, last log lines, a "no progress for N s" flag) and `job_cancel`.
   - No fixed 30 s timeout that kills writers (#4, #5m).
7. **Primitives plus workflows.** Expose small composable primitives (add a track, query a window). Build
   higher-level workflows on them (fan-out, stitch, route-one-net), each with parameters rather than hard-coded
   policy.
8. **Policy as data.** Design rules, fab capabilities and "standards" live in a project file (§2.9), not in code, so
   the same tools serve a hobby 2-layer board and a dense 6-layer one.
9. **Platform hygiene.**
   - Read and write UTF-8 everywhere (#2).
   - Don't take net names through shell arguments that MSYS can mangle (`/Audio/X` became
     `C:/Program Files/Git/...`).
   - Resolve library tables relative to the project.
   - Run DRC against the project's own `.kicad_pro`/`.kicad_dru`, even for copied boards.

## 2.2 Schematic tools

**S1. Faithful symbol placement and pin geometry** (fixes #5, #5b, #5e, #5o, #5p)
- *Why:* Symbols using `extends` failed to load. Embedded copies had shifted pins (QOD shorted to VOUT) and renamed
  pins (`SH`→`S1`). On a rotated jumper, labels were placed 1.27 mm short of the pins.
  - Fixing this took a resync script (`resync_libsyms.py`), a pin-by-pin verifier and manual label moves.
- *What:* `place_symbol` resolves any depth of `extends`, embeds the library text verbatim, and returns the
  absolute pin positions (after rotation/mirror) computed from that text.
- *How:*
  - Parse the library with KiCad's own parser (`kicad-cli sym export svg` or a small S-expression parser).
  - Flatten `extends` the way KiCad does when it saves.
  - Add a regression test that compares `get_schematic_pin_locations` with the netlist `kicad-cli` exports for
    every stock symbol at 0/90/180/270°.

**S2. `assert_connectivity`: verify design intent against the real netlist**
- *Why:* ERC passes even when a label sits on the wrong pin. The only reliable check was exporting the netlist and
  comparing every IC and connector pin to the intended net (`verify_netlist.py`, 478 pins).
- *What:*
  - `assert_connectivity({"U7": {"5": "+3V3", ...}, ...})` returns only mismatches, single-pin nets and unexpected
    no-connects.
  - It accepts net names with or without the hierarchical prefix.
  - It can store the expectation set in the project, so the check can be re-run after every edit at almost no
    token cost.
- *How:* run `kicad-cli sch export netlist` → parse the XML → compare the last path component of each net name.
  Reference implementation: `tools/verify_netlist.py`.

**S3. Label scope management**
- *Why:* I used local labels, then had to promote names used on several sheets to global labels
  (`globalize.py`). `replace=true` left the old global labels behind (#5f).
- *What:*
  - `connect_pins(..., scope: "auto")` picks local or global from whether the net already exists on other sheets.
  - `relabel_net(old, new)` renames a net across the hierarchy.
  - Every "replace" operation removes all label kinds on that pin.

**S4. Bulk property and BOM mapping**
- *Why:* Adding an LCSC number to 185 symbols took a custom mapping script (`lcsc_map.py`). DNP, `in_bom` and
  exclusion flags then had to be kept consistent by hand.
- *What:*
  - `apply_property_map(rules=[{match: {value, footprint_contains, ref_regex}, set: {LCSC: ..., dnp: bool}}])`.
  - It reports unmatched parts.
  - It is idempotent.

**S5. Part sourcing that knows about cost**
- *Why:* The user's first question after "done" was the $132 loading fee. Answering it needed SQL against the
  cached database, by subcategory, with workarounds for operator precedence.
- *What:*
  - `jlc_bom_report(bom)` reports, per line: basic / preferred / extended, stock against quantity × boards, the
    fee, and *suggested basic or preferred alternatives* in the same footprint and parametric class.
  - It also gives a total fee estimate.
  - `jlc_find_alternative(lcsc, constraints)` searches by parameters (value, tolerance, voltage, package).
- *How:* the cached `cdfer.sqlite3` already has `library_type`, `preferred`, `stock`, `subcategory` and
  `attributes`. Expose parametric filters instead of free-text `LIKE` searches.
  - Also fix #1 (DB lock), so the MCP's own database stays usable.

## 2.3 Schematic ↔ board synchronisation

**B1. A real "Update PCB from Schematic"** (fixes #5h, #5i)
- *Why:* `sync_schematic_to_board` only adds missing footprints. It doesn't remove deleted parts, swap changed
  footprints, re-net pads or sync fields. It also fights its own auto-save.
- *What:* `update_board_from_schematic(dry_run)`:
  - adds, removes and swaps footprints, keeping position/rotation/side on a swap;
  - re-nets every pad;
  - syncs Value / Datasheet / custom fields, DNP and exclude-from-BOM;
  - returns a change report plus a schematic-parity result.
- *How:*
  - Implement it from the XML netlist.
  - Escape `/` in generated `unconnected-(...)` names as `{slash}`, as KiCad does.
  - Reference implementation: `tools/update_pcb_from_netlist.py`. It also documents the KiCad 10 SWIG traps:
    `FootprintLoad` only works before `LoadBoard`, and `LIB_ID` is unusable.

**B2. `swap_footprint(ref, lib:fp)`** keeps nets and placement (`tools/swap_footprint.py`). `replace_component`
is advertised but not implemented.

## 2.4 Query and inspection tools (the biggest token savers)

**Q1. `query_region(x0, y0, x1, y1, layers, kinds)`**
- *Why:* Most routing decisions were local ("what's around the codec's right edge?"). Answering them meant dumping
  whole footprints or rendering the whole board.
- *What:* returns pads, tracks, vias, zones and text in a window as a compact table: net, layer, geometry, locked
  flag.
- Reference implementation: `tools/window.py`.

**Q2. `render(region, layers, highlight_nets, scale)`**
- *Why:* `get_board_2d_view` renders the whole board, so a 0.4 mm-pitch QFN is about 10 px wide. I hacked zoomed
  renders by copying the board and replacing its Edge.Cuts (`zoom_copy.py`).
- *What:* crop to a region, choose layers, and draw highlighted nets in colour with everything else greyed.
  - Optionally overlay DRC markers and ratsnest lines for selected nets.

**Q3. `net_report(nets)`**
- *What:* per net, give the length per layer, via count, layers used, plane-split crossings, regions entered,
  width min/max, and whether the net is fully connected.
- *Why:* This one call would have exposed the I2S, +12V and I2C issues in the mixed-signal review immediately.

**Q4. Connectivity at a point or item**
- `connected_items(item | point)` and `why_unconnected(net)` give the islands of a net and the nearest gap between
  them.
- *Why:* Several "unconnected" findings were zone islands, such as the RP2350 exposed pad with no vias after a
  transplant, or a split VREG_LX pour. Each took renders to diagnose.

**Q5. Compact check tools**
- `run_drc(detail="summary", ignore=["silk_*"], region, nets, refill=true)` returns counts by type, the first N
  items with coordinates, and the *delta* against the previous run.
- The same pattern applies to `run_erc`.
- Always run against the project's rules (see principle 9).
- Reference implementation: `tools/drc_summary.py`.

## 2.5 Editing primitives

**E1. Track/via batch API with spec strings**
- *Why:* Hand-routing the codec power pins, the RP2350 pin-64 link and the LOL output needed dozens of exact
  segments.
- *What:*
  - `add_copper([{T: net, layer, width, points: [...]}, {V: net, at, size, drill}], locked)`.
  - `remove_copper(filter: {nets, region, locked, kinds, endpoint_near})`.
  - `unroute(nets, include_locked)`.
  - Every call returns a DRC delta.
- Reference implementations: `add_items.py`, `remove_items.py`, `unroute_nets.py`.
  - Note: Freerouting-imported tracks come back *locked*, so unroute needs an `include_locked` option.

**E2. Placement with collision feedback**
- `move_components([...])` checks courtyards, board edge and keepouts, then returns overlaps and new airwire lengths.
- `place_relative(ref, anchor_ref, offset, rotation)` handles patterns like "decap next to pin".
- Reference implementations: `move_fps.py`, `overlaps.py`.

**E3. Zones and rule areas, done properly** (fixes #5j)
- `add_zone(net, layers, polygon, priority, clearance, min_width, connection, thermal{gap, spoke}, island_mode)`.
- `add_rule_area(layers, polygon, forbid: [tracks, vias, pads, pours, footprints], name)`.
- `edit_zone(...)`.
- `set_pad_zone_connection(refs/pads, solid|thermal|none)`.
- `split_plane(layer, regions: {net: polygon})` builds a multi-net plane in one call.
- Reference implementation: `make_zones.py`. Note the SWIG trap: a new zone's `GetLayerName()` lies.

## 2.6 Routing workflows

**R1. Plane fan-out**
- *Why:* Freerouting cannot reliably connect SMD pads to planes. It left 50–100 plane connections open every run
  until I pre-placed a short stub and a locked via next to every GND/plane pad.
- *What:* `fanout_to_planes(nets, region_policy, via, max_stub, skip_fine_pitch, exposed_pad_grid)`.
  - It includes collision checking against all existing copper.
  - It skips pads that are already served by a nearby via.
- Reference implementation: `fanout_planes.py`.

**R2. Freerouting orchestration that understands Freerouting's limits**
- *Why:* Of the 12 passes, the bad ones failed for reasons the caller had to dig out of logs:
  - pour-only keepouts became hard obstacles, blocking all four pin rows of the RP2350;
  - plane-only connections counted as "unrouted", so the optimiser never ran;
  - one run stalled indefinitely on 811 "unfixable" violations.
- *What:* `autoroute(job)` with these options:
  - `nets` (route a subset only);
  - `lock_existing` (export existing copper as `fix`);
  - `strip_rule_areas: "pour_only" | "all" | "none"`, which removes them from the DSN and restores them after;
  - `forbid_regions_for_nets`;
  - `passes`, `timeout`, and `stall_timeout` (abort if no progress for N s);
  - `import: "if_better"`.
- It returns, through the job API:
  - unrouted connections *as KiCad counts them* (Freerouting's count includes plane connections);
  - the DRC delta;
  - the log tail.
- *How:*
  - Export the DSN with `pcbnew.ExportSpecctraDSN` from a disk-loaded board (#5l).
  - Rewrite the DSN to drop pour-only keepouts.
  - Run Java detached.
  - Import the SES into a copy, check it, then swap it in.

**R3. Single-connection router with constraints**
- *Why:* For one or two connections, a 10-minute autorouter pass is wasteful and uncontrollable. Example: re-routing
  +12 V around the analog corner.
- *What:* `route_connection(net, from, to, width, via, clearance, layers_allowed, avoid_regions, prefer_layer,
  via_cost)`. It is a grid A* router with 45° moves that writes locked copper and returns its path.
  - It took about 1 s for a 57 mm route.
- Reference implementation: `route_one_net.py`.

**R4. Reference-layout transplant**
- *Why:* The best route to a professional RP2350 layout was to copy Raspberry Pi's own regulator, decoupling and
  DVDD-ring routing. Doing it by hand also needed footprint-alignment checks and fixes for things that copy badly:
  - rule areas that turned into router obstacles;
  - narrow pours that split with different pad sizes;
  - exposed-pad vias that were deleted.
- *What:* `transplant_layout(source_board, source_anchor_ref, target_anchor_ref, net_map, region, include:
  [tracks, vias, zones, rule_areas], verify: true)`.
  - Before copying, it checks that every target pad in the region matches the source pad (net + position ±0.05 mm)
    and reports mismatches.
  - That check found my C42 net error and the reversed R21.
  - After copying, it reports pour-island splits and orphaned items.
- Reference implementation: `transplant_rpi_core.py`.

**R5. Ordinary routing helpers** for the cases above: `route_diff_pair` (impedance from the stackup),
`length_match`, and `route_bus` (parallel bundles such as I2S/MCLK).

## 2.7 Finishing tools

**F1. `stitch_ground(grid, edge_pitch, fence_polylines, keep_clear, courtyard_policy)`**
- Builds a stitching grid, an edge ring and via fences along region boundaries.
- Reference implementations: `finish_pours.py`, `via_fence.py`.

**F2. `tidy_silkscreen(text_height, keep_upright, hide_if_no_fit)`** and `add_pin_labels(connector,
labels, side)`
- *Why:* The design ended with 236 silk violations.
- Collision tests must use *stroke* geometry, not bounding boxes: a connector's outline box hid every candidate
  position.
- Reference implementations: `silk_place.py`, `silk_labels.py`.

**F3. `fab_package(fab: "jlcpcb", options)`** produces, in one call:
- Gerbers, drill and map files, zipped;
- BOM and CPL in the fab's format;
- a hand-solder parts list that excludes chosen parts from assembly but keeps them orderable;
- a rotation-correction table for known package offsets;
- a fee and stock report;
- final DRC/ERC/parity reports.
- Reference implementation: `jlc_export.py` plus the export commands in the session log.

**F4. `cleanup_dangling()`**
- Trims dangling stubs back to their junction instead of deleting whole segments. My naive version broke three
  connections and was reverted.

## 2.8 Rules and constraints

**C1. Net classes that match real net names** (#5k, #5q)
- `create_netclass(name, rules, nets|patterns)` validates every pattern against the board.
- It returns the nets each pattern matched and warns on zero matches, which happens when hierarchical names
  (`/Power/+5V`) are missed.
- It persists `netclass_patterns`.

**C2. Custom DRC rules with feedback**
- `add_drc_rule(rule_text)` parses it, reports syntax errors, and reports *how many items it matched* on the
  current board.
- *Why:* Two rules I wrote (`zone_connection`, `min_resolved_spokes`) silently did nothing, and I had to fall back to
  per-pad overrides.

**C3. Design-intent constraints that are checked continuously**
- The ideas from the mixed-signal review, expressed as data so every mutation reports violations:
  - `region "analog" = polygon; allowed_nets = [...]`;
  - `netclass FAST: no_plane_split_crossing, preferred_layer F.Cu`;
  - `net +12V: forbid_region analog`.
- Reference implementation for the checks: `split_audit.py` plus the region-intrusion audit in the session.

## 2.9 A project "standards profile" (policy as data)

The good-practice rules for this board were scattered across my reasoning. They should live in one file that
tools read, so checks are automatic and reusable on other boards. For example, a `standards.yaml`:

```yaml
fab: { name: jlcpcb, layers: 4, min_track: 0.1, min_space: 0.1, min_drill: 0.3, min_annular: 0.1,
       silk_min_height: 0.8, silk_min_width: 0.15, via_default: [0.6, 0.3] }
planes:
  In1: { net: GND, split: false }                     # one solid reference, never split
  In2: { regions: { "+3V3": digital, "+5V": power, GND: analog } }
regions:
  analog:  { polygon: [[66,62],[92,62],[92,56],[100,56],[100,100],[66,100]],
             allowed_nets: ["/Audio/*", "+3V3A", "CODEC_*", "I2S_*", "I2C_*", GND, "+3V3"],
             fence: { pitch: 1.5 }, stitch: 2.5 }
  power:   { polygon: [[0,72],[66,72],[66,100],[0,100]] }
netclasses:
  FAST:   { nets: ["I2S_*", CODEC_MCLK, "USB_*", "QSPI_*"], no_plane_split_crossing: true, prefer: F.Cu }
  PWR_HI: { width: 1.0, nets: ["/Power/+12V", "/Power/+5V", "+5V_*", "+12V_*"] }
checks:
  every_gnd_smd_pad_has_via_within: 1.3
  exposed_pads_have_via_array: true
  decoupling_within_mm: 3             # each IC supply pin -> nearest same-net cap
  reference_layouts: [{ source: rpi_rp2350b_minimal, anchor: U7 }]
```

The MCP should load this file and add any violations to every DRC delta. The same tools then enforce "hobby
2-layer" or "dense 6-layer" standards just by swapping the profile.

## 2.10 Infrastructure

- **I1. Snapshots and undo.** Run `snapshot(label)` automatically before every mutation, plus `restore(label)` and
  `diff(label_a, label_b)` (items added, removed and moved). I ran a manual `bak.sh` before nearly every MCP call
  because of #4.
- **I2. Session state report.** `project_status()` returns sheet and component counts, unrouted count, DRC/ERC
  counts, parity, last snapshot and dirty flags in about 10 lines. That's the cheapest way to re-orient after
  context compaction.
- **I3. Script escape hatch.** `run_pcbnew_script(code, board, save)` runs in KiCad's Python against a fresh disk
  load. It should handle the KiCad 10 SWIG traps listed in #5r (pre-loading footprints, never trusting
  `GetFPID`). Many of my tools are 30-line scripts that would fit here.
- **I4. Tool discoverability.** Remove or hide unimplemented tools (`replace_component`, `add_zone`). Put the
  units, coordinate origin and return shape in each tool description.

## 2.11 Priorities (highest value per unit of effort)

| # | Feature | Fixes / replaces | Expected saving |
|---|---|---|---|
| 1 | Atomic writes + verbatim symbols (principles 1–2, S1) | #4, #5c, #5e, #5p | prevents silent wrong connections and data loss |
| 2 | Summary/delta outputs on DRC, ERC and queries (principles 3–4, Q5) | — | largest token reduction in every loop |
| 3 | `query_region` + cropped `render` (Q1, Q2) | `window.py`, `zoom_copy.py` | most local layout questions answered in one call |
| 4 | `update_board_from_schematic` (B1) | #5h, `update_pcb_from_netlist.py` | every schematic change |
| 5 | `assert_connectivity` (S2) | `verify_netlist.py` | cheap correctness gate after every edit |
| 6 | Plane fan-out + Freerouting orchestration (R1, R2) | 12 slow passes | autorouting that converges and reports real reasons |
| 7 | Zones/rule areas/net classes that work (E3, C1, C2) | #5j, #5k, #5q | removes manual JSON and S-expression edits |
| 8 | `route_connection`, `transplant_layout`, `stitch_ground`, `tidy_silkscreen`, `fab_package` | the remaining tools | turns finishing into a few parameterised calls |
| 9 | Standards profile + continuous checks (2.9, C3) | mixed-signal review | design quality enforced automatically |
| 10 | JLC cost-aware sourcing (S5) | fee review | catches assembly-cost surprises before layout |

## 2.12 Index of reference implementations (`CD_Mainboard/tools/`)

| Script | Feature it prototypes |
|---|---|
| `verify_netlist.py` | S2 `assert_connectivity` |
| `globalize.py`, `resync_libsyms.py`, `extract_sym.py`, `sexp_tools.py` | S1/S3 symbol fidelity, label scope |
| `lcsc_map.py`, `jlc_export.py` | S4 property map, F3 fab package (with hand-solder split) |
| `update_pcb_from_netlist.py`, `swap_footprint.py` | B1, B2 |
| `window.py`, `pads.py`, `fp_dump.py`, `overlaps.py`, `zoom_copy.py`, `drc_summary.py`, `split_audit.py` | Q1–Q5, C3 |
| `add_items.py`, `remove_items.py`, `unroute_nets.py`, `move_fps.py` | E1, E2 |
| `make_zones.py` | E3 split planes |
| `fanout_planes.py`, `route_one_net.py`, `transplant_rpi_core.py` | R1, R3, R4 |
| `finish_pours.py`, `via_fence.py`, `silk_place.py`, `silk_labels.py` | F1, F2 |
