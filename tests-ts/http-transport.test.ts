import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { afterEach, describe, expect, it } from "vitest";
import { KiCADMcpServer } from "../src/server.js";
import {
  DEFAULT_HTTP_HOST,
  DEFAULT_HTTP_PORT,
  resolveHttpOptions,
  type HttpOptions,
} from "../src/http-transport.js";
import { isLongRunningTool } from "../src/background-calls.js";

// A stand-in for the Python worker: echoes each command and its params.
// FAKE_SLOW="cmd:ms,..." delays a command; FAKE_FAIL="cmd,..." answers success:false.
const FAKE_WORKER = `
const slow = Object.fromEntries((process.env.FAKE_SLOW || "").split(",").filter(Boolean).map((e) => {
  const [c, ms] = e.split(":"); return [c, Number(ms)];
}));
const fail = new Set((process.env.FAKE_FAIL || "").split(",").filter(Boolean));
process.stdout.write(JSON.stringify({ type: "ready" }) + "\\n");
let buffer = "";
const queue = [];
let busy = false;
function next() {
  if (busy || queue.length === 0) return;
  busy = true;
  const request = queue.shift();
  setTimeout(() => {
    const reply = fail.has(request.command)
      ? { success: false, message: request.command + " failed on purpose" }
      : { success: true, command: request.command, params: request.params };
    reply._requestId = request.requestId;
    process.stdout.write(JSON.stringify(reply) + "\\n");
    busy = false;
    next();
  }, slow[request.command] || 0);
}
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  while (buffer.includes("\\n")) {
    const index = buffer.indexOf("\\n");
    const line = buffer.slice(0, index);
    buffer = buffer.slice(index + 1);
    if (line.trim()) queue.push(JSON.parse(line));
  }
  next();
});
`;

const servers: KiCADMcpServer[] = [];
const clients: Client[] = [];
const directories: string[] = [];
const savedEnv = { ...process.env };

afterEach(async () => {
  for (const client of clients.splice(0)) await client.close().catch(() => undefined);
  for (const server of servers.splice(0)) await server.stop();
  for (const dir of directories.splice(0)) rmSync(dir, { recursive: true, force: true });
  for (const key of ["KICAD_PYTHON", "FAKE_SLOW", "FAKE_FAIL", "KICAD_MCP_HTTP_TOKEN"]) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

function httpOptions(extra: Partial<HttpOptions> = {}): HttpOptions {
  return {
    host: "127.0.0.1",
    port: 0,
    maxSessions: 32,
    sessionIdleMs: 60_000,
    ...extra,
  };
}

async function startServer(
  options: {
    http?: Partial<HttpOptions>;
    deferAfterMs?: number;
    slow?: string;
    fail?: string;
  } = {},
) {
  const directory = mkdtempSync(join(tmpdir(), "kicad-mcp-http-"));
  directories.push(directory);
  const scriptPath = join(directory, "worker.cjs");
  writeFileSync(scriptPath, FAKE_WORKER);
  process.env.KICAD_PYTHON = process.execPath;
  if (options.slow) process.env.FAKE_SLOW = options.slow;
  if (options.fail) process.env.FAKE_FAIL = options.fail;

  const server = new KiCADMcpServer(scriptPath, "error", {
    transport: "http",
    http: httpOptions(options.http),
    deferAfterMs: options.deferAfterMs ?? 0,
  });
  (server as any).validatePrerequisites = async () => true;
  servers.push(server);
  await server.start();
  const url: string = (server as any).httpServer.url;
  return { server, url };
}

async function connect(url: string, headers: Record<string, string> = {}) {
  const client = new Client({ name: "http-test", version: "0.0.0" });
  await client.connect(
    new StreamableHTTPClientTransport(new URL(url), { requestInit: { headers } }),
  );
  clients.push(client);
  return client;
}

function payload(result: any, index = 0): any {
  return JSON.parse(result.content[index].text);
}

describe("resolveHttpOptions", () => {
  it("defaults to loopback on port 8808 with no token", () => {
    const options = resolveHttpOptions({});
    expect(options.host).toBe(DEFAULT_HTTP_HOST);
    expect(options.port).toBe(DEFAULT_HTTP_PORT);
    expect(options.token).toBeUndefined();
  });

  it("reads the environment, and command-line values win", () => {
    const env = {
      KICAD_MCP_HTTP_PORT: "9123",
      KICAD_MCP_HTTP_HOST: "0.0.0.0",
      KICAD_MCP_HTTP_TOKEN: "secret",
      KICAD_MCP_HTTP_ALLOWED_HOSTS: "host.docker.internal, kicad.local",
    };
    const options = resolveHttpOptions(env, { port: 9200 });
    expect(options.port).toBe(9200);
    expect(options.host).toBe("0.0.0.0");
    expect(options.allowedHosts).toEqual(["host.docker.internal", "kicad.local"]);
  });

  it("refuses to bind beyond loopback without a token", () => {
    expect(() => resolveHttpOptions({ KICAD_MCP_HTTP_HOST: "0.0.0.0" })).toThrow(/TOKEN/);
  });
});

describe("MCP over Streamable HTTP", () => {
  it("serves initialize, tools/list and tools/call, with instructions", async () => {
    const { url } = await startServer();
    const client = await connect(url);
    expect(client.getInstructions()).toMatch(/async:true/);
    const { tools } = await client.listTools();
    expect(tools.length).toBeGreaterThan(250);
    expect(tools.find((t) => t.name === "job_status")?.annotations?.readOnlyHint).toBe(true);
    const result: any = await client.callTool({ name: "get_board_info", arguments: {} });
    expect(payload(result)).toMatchObject({ success: true, command: "get_board_info" });
  });

  it("keeps sessions apart but shares the one KiCad worker", async () => {
    const { server, url } = await startServer();
    const a = await connect(url);
    const b = await connect(url);
    expect((server as any).httpServer.sessionCount).toBe(2);
    const [ra, rb]: any[] = await Promise.all([
      a.callTool({ name: "get_board_info", arguments: {} }),
      b.callTool({ name: "get_layer_list", arguments: {} }),
    ]);
    expect(payload(ra).command).toBe("get_board_info");
    expect(payload(rb).command).toBe("get_layer_list");
  });

  it("answers /health, and rejects requests without a session or initialize", async () => {
    const { url } = await startServer();
    const base = url.replace(/\/mcp$/, "");
    const health = await (await fetch(`${base}/health`)).json();
    expect(health).toMatchObject({ status: "ok", ready: true, transport: "streamable-http" });

    const notInit = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
    });
    expect(notInit.status).toBe(400);
    const unknown = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        "mcp-session-id": "nope",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
    });
    expect(unknown.status).toBe(404);
  });

  it("rejects a foreign Host header (DNS rebinding)", async () => {
    const { url } = await startServer();
    const { request } = await import("node:http");
    const status = await new Promise<number>((resolve, reject) => {
      const target = new URL(url.replace(/\/mcp$/, "/health"));
      const req = request(
        {
          host: target.hostname,
          port: target.port,
          path: target.pathname,
          headers: { host: "evil.example" },
        },
        (res) => resolve(res.statusCode ?? 0),
      );
      req.on("error", reject);
      req.end();
    });
    expect(status).toBe(403);
  });

  it("requires the bearer token when one is set", async () => {
    const { url } = await startServer({ http: { token: "s3cret" } });
    await expect(connect(url)).rejects.toThrow();
    const client = await connect(url, { Authorization: "Bearer s3cret" });
    const result: any = await client.callTool({ name: "get_board_info", arguments: {} });
    expect(payload(result).success).toBe(true);
  });

  it("evicts the least recently used session past the limit", async () => {
    const { server, url } = await startServer({ http: { maxSessions: 2 } });
    await connect(url);
    await connect(url);
    await connect(url);
    expect((server as any).httpServer.sessionCount).toBe(2);
  });
});

describe("background calls", () => {
  it("gives long tools an async parameter, and leaves autoroute's own async alone", async () => {
    expect(isLongRunningTool("run_drc")).toBe(true);
    expect(isLongRunningTool("get_board_info")).toBe(false);
    expect(isLongRunningTool("job_status")).toBe(false);
    const { url } = await startServer();
    const client = await connect(url);
    const { tools } = await client.listTools();
    const byName = new Map(tools.map((t) => [t.name, t]));
    expect(byName.get("run_drc")?.inputSchema.properties).toHaveProperty("async");
    expect(byName.get("run_drc")?.description).toMatch(/Long-running/);
    expect(byName.get("get_board_info")?.inputSchema.properties ?? {}).not.toHaveProperty("async");
    expect(byName.get("job_status")?.inputSchema.properties).toHaveProperty("waitSeconds");

    // autoroute's async is its own (the orchestrated pipeline): passed through to Python.
    const routed: any = await client.callTool({ name: "autoroute", arguments: { async: true } });
    expect(payload(routed)).toMatchObject({ command: "autoroute", params: { async: true } });
  });

  it("async:true returns a jobId at once; job_status returns the tool's reply", async () => {
    const { url } = await startServer({ slow: "run_drc:400" });
    const client = await connect(url);
    const t0 = Date.now();
    const started: any = await client.callTool({ name: "run_drc", arguments: { async: true } });
    expect(Date.now() - t0).toBeLessThan(300);
    const job = payload(started);
    expect(job).toMatchObject({ success: true, async: true, state: "running", tool: "run_drc" });
    expect(job.jobId).toMatch(/^bg-/);

    const running: any = await client.callTool({
      name: "job_status",
      arguments: { jobId: job.jobId },
    });
    expect(payload(running).state).toBe("running");

    const done: any = await client.callTool({
      name: "job_status",
      arguments: { jobId: job.jobId, waitSeconds: 5 },
    });
    expect(payload(done)).toMatchObject({ state: "succeeded", tool: "run_drc" });
    // The tool's own reply follows the status; `async` is not passed to Python.
    expect(payload(done, 1)).toMatchObject({ success: true, command: "run_drc" });
    expect(payload(done, 1).params).not.toHaveProperty("async");

    const list: any = await client.callTool({ name: "job_list", arguments: { kind: "call" } });
    expect(payload(list).jobs.map((j: any) => j.jobId)).toContain(job.jobId);
  });

  it("defers a call that runs past the threshold, and other sessions can follow it", async () => {
    const { url } = await startServer({ deferAfterMs: 150, slow: "export_gerbers:600" });
    const a = await connect(url);
    const quick: any = await a.callTool({ name: "get_board_info", arguments: {} });
    expect(payload(quick).command).toBe("get_board_info");

    const deferred: any = await a.callTool({
      name: "export_gerbers",
      arguments: { outputDir: "/tmp/g" },
    });
    const job = payload(deferred);
    expect(job).toMatchObject({ async: true, deferred: true, tool: "export_gerbers" });
    expect(job.message).toMatch(/Do not call it again/);

    // A different session (Executor opens one per call) sees the same job.
    const b = await connect(url);
    const done: any = await b.callTool({
      name: "job_status",
      arguments: { jobId: job.jobId, waitSeconds: 5 },
    });
    expect(payload(done).state).toBe("succeeded");
    expect(payload(done, 1)).toMatchObject({
      command: "export_gerbers",
      params: { outputDir: "/tmp/g" },
    });
  });

  it("reports a failed background call, which cannot be cancelled or applied", async () => {
    const { url } = await startServer({ fail: "refill_zones" });
    const client = await connect(url);
    const job = payload(
      await client.callTool({ name: "refill_zones", arguments: { async: true } }),
    );
    const done: any = await client.callTool({
      name: "job_status",
      arguments: { jobId: job.jobId, waitSeconds: 5 },
    });
    expect(done.isError).toBeFalsy(); // the status query itself worked
    expect(payload(done).state).toBe("failed");
    expect(payload(done, 1)).toMatchObject({ success: false });

    const cancel: any = await client.callTool({
      name: "job_cancel",
      arguments: { jobId: job.jobId },
    });
    expect(cancel.isError).toBe(true);
    const apply: any = await client.callTool({
      name: "job_apply",
      arguments: { jobId: job.jobId },
    });
    expect(apply.isError).toBe(true);
    const unknown: any = await client.callTool({
      name: "job_status",
      arguments: { jobId: "bg-0000000000" },
    });
    expect(unknown.isError).toBe(true);
  });

  it("does not defer over stdio unless asked (deferAfterMs 0)", async () => {
    const { url } = await startServer({ deferAfterMs: 0, slow: "run_drc:300" });
    const client = await connect(url);
    const result: any = await client.callTool({ name: "run_drc", arguments: {} });
    expect(payload(result)).toMatchObject({ success: true, command: "run_drc" });
  });
});
