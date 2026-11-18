/**
 * Streamable HTTP transport (MCP 2025-03-26+), for clients that cannot start a
 * local process, such as Executor (https://github.com/UsefulSoftwareCo/executor).
 *
 * One HTTP endpoint (`/mcp`) serves any number of MCP sessions. Each session
 * gets its own McpServer (tool visibility and toolboxes are per session), and
 * every session drives the same KiCad worker, so they all see the same open
 * project. Some clients open a session per call and never close it, so idle
 * sessions are closed after `sessionIdleMs` and at most `maxSessions` are kept.
 *
 * Security: the server can write files and run Python against the board, so it
 * binds to 127.0.0.1 by default and checks the Host header (DNS rebinding).
 * Binding anywhere else requires a bearer token.
 */
import { randomUUID, timingSafeEqual } from "node:crypto";
import type { Server as NodeHttpServer } from "node:http";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import {
  hostHeaderValidation,
  localhostHostValidation,
} from "@modelcontextprotocol/sdk/server/middleware/hostHeaderValidation.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import express, { type NextFunction, type Request, type Response } from "express";
import { logger } from "./logger.js";

export const TRANSPORT_ENV = "KICAD_MCP_TRANSPORT";
export const HTTP_PORT_ENV = "KICAD_MCP_HTTP_PORT";
export const HTTP_HOST_ENV = "KICAD_MCP_HTTP_HOST";
export const HTTP_TOKEN_ENV = "KICAD_MCP_HTTP_TOKEN";
export const HTTP_ALLOWED_HOSTS_ENV = "KICAD_MCP_HTTP_ALLOWED_HOSTS";
export const HTTP_MAX_SESSIONS_ENV = "KICAD_MCP_HTTP_MAX_SESSIONS";
export const HTTP_SESSION_IDLE_ENV = "KICAD_MCP_HTTP_SESSION_IDLE_MS";

export const DEFAULT_HTTP_PORT = 8808;
export const DEFAULT_HTTP_HOST = "127.0.0.1";
export const MCP_PATH = "/mcp";

export interface HttpOptions {
  host: string;
  port: number;
  /** Bearer token required on every request; required when host is not loopback. */
  token?: string;
  /** Host header values accepted; default: the loopback names when bound to loopback. */
  allowedHosts?: string[];
  maxSessions: number;
  sessionIdleMs: number;
}

const LOOPBACK = new Set(["127.0.0.1", "localhost", "::1"]);

export function isLoopbackHost(host: string): boolean {
  return LOOPBACK.has(host);
}

function positiveInt(raw: string | undefined, fallback: number): number {
  const n = raw === undefined || raw.trim() === "" ? NaN : Number(raw);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}

/**
 * HTTP settings from the environment, overridden by command-line values.
 * Throws when the combination is unsafe (non-loopback without a token).
 */
export function resolveHttpOptions(
  env: Record<string, string | undefined> = process.env,
  overrides: { host?: string; port?: number } = {},
): HttpOptions {
  const host = overrides.host ?? (env[HTTP_HOST_ENV]?.trim() || DEFAULT_HTTP_HOST);
  const port = overrides.port ?? positiveInt(env[HTTP_PORT_ENV], DEFAULT_HTTP_PORT);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`Invalid HTTP port: ${port}`);
  }
  const token = env[HTTP_TOKEN_ENV]?.trim() || undefined;
  const allowedHosts = env[HTTP_ALLOWED_HOSTS_ENV]?.split(",")
    .map((h) => h.trim())
    .filter(Boolean);
  if (!isLoopbackHost(host) && !token) {
    throw new Error(
      `Refusing to serve MCP over HTTP on ${host} without ${HTTP_TOKEN_ENV}: the tools can ` +
        "write files and run Python against the board. Set a token, or bind to 127.0.0.1.",
    );
  }
  return {
    host,
    port,
    token,
    allowedHosts: allowedHosts && allowedHosts.length > 0 ? allowedHosts : undefined,
    maxSessions: positiveInt(env[HTTP_MAX_SESSIONS_ENV], 32),
    sessionIdleMs: positiveInt(env[HTTP_SESSION_IDLE_ENV], 30 * 60_000),
  };
}

interface Session {
  transport: StreamableHTTPServerTransport;
  server: McpServer;
  lastSeen: number;
}

export interface HttpStatus {
  ready: () => boolean;
}

/** A running HTTP endpoint; `close()` ends every session and the listener. */
export class McpHttpServer {
  private sessions = new Map<string, Session>();
  private listener?: NodeHttpServer;
  private sweeper?: NodeJS.Timeout;

  constructor(
    private readonly options: HttpOptions,
    private readonly createServer: () => McpServer,
    private readonly status: HttpStatus,
  ) {}

  /** Number of open MCP sessions (for tests and /health). */
  get sessionCount(): number {
    return this.sessions.size;
  }

  /** The port actually bound (useful with port 0 in tests). */
  get port(): number {
    const address = this.listener?.address();
    return typeof address === "object" && address !== null ? address.port : this.options.port;
  }

  get url(): string {
    const host = this.options.host.includes(":") ? `[${this.options.host}]` : this.options.host;
    return `http://${host}:${this.port}${MCP_PATH}`;
  }

  async listen(): Promise<void> {
    const app = this.buildApp();
    await new Promise<void>((resolve, reject) => {
      const listener = app.listen(this.options.port, this.options.host, () => resolve());
      listener.once("error", reject);
      this.listener = listener;
    });
    this.sweeper = setInterval(
      () => this.closeIdle(),
      Math.min(this.options.sessionIdleMs, 60_000),
    );
    this.sweeper.unref();
    logger.info(`MCP over HTTP listening at ${this.url}`);
  }

  async close(): Promise<void> {
    if (this.sweeper) clearInterval(this.sweeper);
    for (const id of [...this.sessions.keys()]) await this.closeSession(id);
    const listener = this.listener;
    this.listener = undefined;
    if (listener) {
      listener.closeAllConnections?.();
      await new Promise<void>((resolve) => listener.close(() => resolve()));
    }
  }

  private buildApp() {
    const app = express();
    app.disable("x-powered-by");
    // DNS rebinding protection: a page in the user's browser must not be able
    // to reach this server through a hostname it controls.
    if (this.options.allowedHosts) {
      app.use(hostHeaderValidation(this.options.allowedHosts));
    } else if (isLoopbackHost(this.options.host)) {
      app.use(localhostHostValidation());
    }
    if (this.options.token) app.use((req, res, next) => this.checkToken(req, res, next));
    // Tool calls can carry large payloads (batch edits, symbol definitions).
    app.use(express.json({ limit: "50mb" }));

    app.get("/health", (_req, res) => {
      res.json({
        status: "ok",
        ready: this.status.ready(),
        transport: "streamable-http",
        endpoint: MCP_PATH,
        sessions: this.sessions.size,
      });
    });
    app.post(MCP_PATH, (req, res) => this.handlePost(req, res));
    app.get(MCP_PATH, (req, res) => this.handleSessionRequest(req, res));
    app.delete(MCP_PATH, (req, res) => this.handleSessionRequest(req, res));
    return app;
  }

  private checkToken(req: Request, res: Response, next: NextFunction): void {
    const header = req.headers.authorization ?? "";
    const presented = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
    const expected = Buffer.from(this.options.token ?? "");
    const given = Buffer.from(presented);
    if (given.length === expected.length && timingSafeEqual(given, expected)) {
      next();
      return;
    }
    res
      .status(401)
      .set("WWW-Authenticate", 'Bearer realm="kicad-mcp"')
      .json(rpcError(-32001, "Unauthorized: send Authorization: Bearer <KICAD_MCP_HTTP_TOKEN>"));
  }

  private async handlePost(req: Request, res: Response): Promise<void> {
    try {
      const sessionId = header(req, "mcp-session-id");
      let session = sessionId ? this.sessions.get(sessionId) : undefined;
      if (sessionId && !session) {
        res.status(404).json(rpcError(-32001, `Session not found: ${sessionId}`));
        return;
      }
      if (!session) {
        if (!isInitializeRequest(req.body)) {
          res
            .status(400)
            .json(rpcError(-32000, "Bad Request: send initialize first (no Mcp-Session-Id)"));
          return;
        }
        session = await this.openSession();
      }
      session.lastSeen = Date.now();
      await session.transport.handleRequest(req, res, req.body);
    } catch (error) {
      logger.error(`MCP HTTP request failed: ${error}`);
      if (!res.headersSent) res.status(500).json(rpcError(-32603, "Internal server error"));
    }
  }

  /** GET (server-to-client stream) and DELETE (end session) for an existing session. */
  private async handleSessionRequest(req: Request, res: Response): Promise<void> {
    const sessionId = header(req, "mcp-session-id");
    const session = sessionId ? this.sessions.get(sessionId) : undefined;
    if (!session) {
      res.status(sessionId ? 404 : 400).json(rpcError(-32000, "Missing or unknown Mcp-Session-Id"));
      return;
    }
    session.lastSeen = Date.now();
    try {
      await session.transport.handleRequest(req, res);
    } catch (error) {
      logger.error(`MCP HTTP ${req.method} failed: ${error}`);
      if (!res.headersSent) res.status(500).json(rpcError(-32603, "Internal server error"));
    }
  }

  private async openSession(): Promise<Session> {
    // Make room: drop the least recently used session past the cap.
    while (this.sessions.size >= this.options.maxSessions) {
      let oldest: [string, Session] | undefined;
      for (const entry of this.sessions) {
        if (!oldest || entry[1].lastSeen < oldest[1].lastSeen) oldest = entry;
      }
      if (!oldest) break;
      logger.info(`Closing least recently used MCP session ${oldest[0]} (limit reached)`);
      await this.closeSession(oldest[0]);
    }
    const server = this.createServer();
    const session: Session = {
      server,
      lastSeen: Date.now(),
      transport: new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => {
          this.sessions.set(id, session);
          logger.info(`MCP HTTP session opened: ${id} (${this.sessions.size} open)`);
        },
        onsessionclosed: (id) => {
          this.sessions.delete(id);
          logger.info(`MCP HTTP session closed by client: ${id}`);
        },
      }),
    };
    session.transport.onclose = () => {
      const id = session.transport.sessionId;
      if (id && this.sessions.get(id) === session) this.sessions.delete(id);
    };
    await server.connect(session.transport);
    return session;
  }

  private async closeSession(id: string): Promise<void> {
    const session = this.sessions.get(id);
    this.sessions.delete(id);
    if (!session) return;
    try {
      await session.server.close();
    } catch (error) {
      logger.warn(`Closing MCP session ${id}: ${error}`);
    }
  }

  private closeIdle(): void {
    const cutoff = Date.now() - this.options.sessionIdleMs;
    for (const [id, session] of this.sessions) {
      if (session.lastSeen < cutoff) {
        logger.info(`Closing idle MCP session ${id}`);
        void this.closeSession(id);
      }
    }
  }
}

function header(req: Request, name: string): string | undefined {
  const value = req.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function rpcError(code: number, message: string) {
  return { jsonrpc: "2.0", error: { code, message }, id: null };
}
