#!/usr/bin/env node
/**
 * generate-python-tool-schemas.mjs — refresh python/schemas/generated_tool_schemas.json.
 *
 * The Python backend can also run as an MCP server on its own, and its
 * tools/list comes from python/schemas/tool_schemas.py. That file is written
 * by hand and covers only some of the commands, so every newer tool was
 * advertised there with an empty input schema. The TypeScript server's zod
 * definitions are the schemas MCP clients actually validate against, so this
 * script asks the built server for its tools/list and stores those JSON
 * schemas; tool_schemas.py fills every tool it does not describe itself from
 * the stored file. Tools answered by TypeScript alone (tool discovery, the
 * parts registry) are left out: Python has no command for them.
 *
 * Usage
 * -----
 *   npm run build
 *   npm run schemas:python            # writes the JSON file
 *   npm run schemas:python -- --check # exit 1 if the file is out of date
 */

import { readFileSync, readdirSync, writeFileSync, existsSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, "python", "schemas", "generated_tool_schemas.json");
const check = process.argv.includes("--check");

/** Names of tools whose server.tool() handler calls the Python backend. */
function toolsThatCallPython() {
  const names = new Set();
  const dir = join(ROOT, "src", "tools");
  for (const file of readdirSync(dir).filter((f) => f.endsWith(".ts"))) {
    const src = readFileSync(join(dir, file), "utf8");
    const starts = [...src.matchAll(/server\.tool\(\s*"([^"]+)"\s*,/g)];
    starts.forEach((m, i) => {
      const end = i + 1 < starts.length ? starts[i + 1].index : src.length;
      if (src.slice(m.index, end).includes("callKicadScript(")) names.add(m[1]);
    });
  }
  return names;
}
const pythonTools = toolsThatCallPython();

const env = { ...process.env, LOG_LEVEL: "error" };
// Every tool, as a client sees it in the default write mode with all
// toolboxes open (the toolbox controls are TypeScript-only and stay hidden).
delete env.KICAD_MCP_OPERATING_MODE;
delete env.KICAD_MCP_TOOLBOXES;

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [join(ROOT, "dist", "index.js")],
  env,
  stderr: "ignore",
});
const client = new Client({ name: "schema-generator", version: "1.0.0" });
await client.connect(transport);
let tools;
try {
  ({ tools } = await client.listTools());
} finally {
  await client.close();
}

const schemas = {};
for (const tool of [...tools].sort((a, b) => a.name.localeCompare(b.name))) {
  if (!pythonTools.has(tool.name)) continue;
  const entry = { name: tool.name };
  if (tool.title) entry.title = tool.title;
  entry.description = tool.description ?? "";
  entry.inputSchema = tool.inputSchema;
  schemas[tool.name] = entry;
}
const text = JSON.stringify(schemas, null, 2) + "\n";

if (check) {
  const current = existsSync(OUT) ? readFileSync(OUT, "utf8").replace(/\r\n/g, "\n") : "";
  if (current !== text) {
    console.error(`${OUT} is out of date; run npm run build && npm run schemas:python`);
    process.exit(1);
  }
  console.log(`${Object.keys(schemas).length} tool schemas up to date`);
} else {
  writeFileSync(OUT, text);
  console.log(`Wrote ${Object.keys(schemas).length} tool schemas to ${OUT}`);
}
process.exit(0);
