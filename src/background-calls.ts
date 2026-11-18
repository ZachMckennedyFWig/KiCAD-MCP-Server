/**
 * Background calls: long tools answer with a job id instead of holding the
 * client's request open.
 *
 * Many MCP clients give up on a tool call long before a KiCad command is done:
 * Executor cancels an upstream call after 30 s by default, and other clients
 * often stop at 60 s. Autoroute, DRC, zone refill, Gerber export and the batch
 * schematic tools routinely run for minutes. Two mechanisms keep those calls
 * usable:
 *
 * - **`async: true`.** Every long-running tool (see `isLongRunningTool`) gains
 *   an optional `async` parameter. With it the call returns
 *   `{jobId, state: "running"}` at once, and `job_status` returns the tool's
 *   own reply when it is done. `autoroute` keeps its own `async` (the
 *   orchestrated pipeline with `job_apply`); it is left alone.
 * - **Deferral.** With `deferAfterMs` set (the HTTP transport sets it by
 *   default), any call still running after that long is answered with a job
 *   id too, and finishes in the background.
 *
 * The work is not interrupted either way: the Python worker runs commands one
 * at a time, so a later call simply queues behind it. Jobs belong to the
 * process, not to an MCP session, because some clients (Executor) open a new
 * session for every call.
 */
import { randomBytes } from "node:crypto";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { computeCommandTimeout, LONG_COMMAND_TIMEOUT_MS } from "./command-timeout.js";
import { flagFailure } from "./tools/failure-as-error.js";

/** Background-call job ids start with this; Python's own jobs are `<kind>-<hex>`. */
export const BACKGROUND_JOB_PREFIX = "bg-";

/** Longest single wait job_status may block for: below Executor's 30 s call timeout. */
export const MAX_STATUS_WAIT_SECONDS = 20;

/** Default deferral threshold for the HTTP transport (below Executor's 30 s). */
export const DEFAULT_HTTP_DEFER_AFTER_MS = 20_000;

/** Tools never deferred or given `async`: they report on jobs, and must answer. */
const NEVER_BACKGROUND = new Set(["job_status", "job_list", "job_cancel", "job_apply"]);

type Reply = { content: any[]; isError?: boolean; [key: string]: unknown };

export type BackgroundState = "running" | "succeeded" | "failed";

export interface BackgroundJob {
  jobId: string;
  tool: string;
  state: BackgroundState;
  /** True when the job began as a normal call that ran past the deferral threshold. */
  deferred: boolean;
  startedAt: number;
  finishedAt?: number;
  reply?: Reply;
  error?: string;
}

/**
 * Whether `tool` gets an `async` parameter: its command timeout (with default
 * parameters) is the long allowance or more. Tool names are the Python
 * command names, which command-timeout.ts keys on.
 */
export function isLongRunningTool(tool: string): boolean {
  if (NEVER_BACKGROUND.has(tool)) return false;
  return computeCommandTimeout(tool, {}, {}) >= LONG_COMMAND_TIMEOUT_MS;
}

function textReply(payload: Record<string, unknown>): Reply {
  return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
}

/** The process-wide store of background calls. */
export class BackgroundCalls {
  private jobs = new Map<string, BackgroundJob>();
  /** Waiters per job, resolved when it finishes. */
  private waiters = new Map<string, Array<() => void>>();

  constructor(private readonly keepFinished = 200) {}

  /** Track `work` as a background job for `tool`. */
  track(tool: string, work: Promise<unknown>, deferred: boolean): BackgroundJob {
    const job: BackgroundJob = {
      jobId: `${BACKGROUND_JOB_PREFIX}${randomBytes(5).toString("hex")}`,
      tool,
      state: "running",
      deferred,
      startedAt: Date.now(),
    };
    this.jobs.set(job.jobId, job);
    work.then(
      (reply) => {
        const flagged = flagFailure(reply as Reply);
        job.reply = flagged;
        job.state = flagged?.isError === true ? "failed" : "succeeded";
        this.finish(job);
      },
      (error: unknown) => {
        job.error = error instanceof Error ? error.message : String(error);
        job.state = "failed";
        this.finish(job);
      },
    );
    return job;
  }

  get(jobId: string): BackgroundJob | undefined {
    return this.jobs.get(jobId);
  }

  list(): BackgroundJob[] {
    return [...this.jobs.values()];
  }

  /** Number of jobs still running (they hold, or are queued for, the worker). */
  running(): number {
    let n = 0;
    for (const job of this.jobs.values()) if (job.state === "running") n += 1;
    return n;
  }

  /** Resolve when the job finishes or `timeoutMs` passes, whichever is first. */
  async wait(jobId: string, timeoutMs: number): Promise<void> {
    const job = this.jobs.get(jobId);
    if (!job || job.state !== "running" || timeoutMs <= 0) return;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(done, timeoutMs);
      function done() {
        clearTimeout(timer);
        resolve();
      }
      const list = this.waiters.get(jobId) ?? [];
      list.push(done);
      this.waiters.set(jobId, list);
    });
  }

  /** The reply a tool gives when it hands its work to the background. */
  startedReply(job: BackgroundJob, deferAfterMs?: number): Reply {
    const message = job.deferred
      ? `${job.tool} is still running after ${Math.round((deferAfterMs ?? 0) / 1000)} s and ` +
        "continues in the background. Do not call it again: call job_status with this jobId " +
        `(waitSeconds up to ${MAX_STATUS_WAIT_SECONDS}) until state is succeeded or failed; ` +
        "the result is the tool's normal reply."
      : `Started ${job.tool} in the background. Call job_status with this jobId ` +
        `(waitSeconds up to ${MAX_STATUS_WAIT_SECONDS}) until state is succeeded or failed; ` +
        "the result is the tool's normal reply.";
    return textReply({
      success: true,
      async: true,
      deferred: job.deferred,
      jobId: job.jobId,
      tool: job.tool,
      state: job.state,
      message,
    });
  }

  /** job_status's reply for a background call: status first, then the tool's own content. */
  statusReply(job: BackgroundJob, includeResult = true): Reply {
    const elapsed = ((job.finishedAt ?? Date.now()) - job.startedAt) / 1000;
    const status: Record<string, unknown> = {
      success: true,
      jobId: job.jobId,
      kind: "call",
      tool: job.tool,
      state: job.state,
      deferred: job.deferred,
      elapsedSeconds: Math.round(elapsed * 10) / 10,
    };
    if (job.state === "running") {
      status.message =
        `${job.tool} is still running. Call job_status again ` +
        `(waitSeconds up to ${MAX_STATUS_WAIT_SECONDS}); other tool calls queue behind it.`;
      return textReply(status);
    }
    if (job.error !== undefined) {
      status.error = job.error;
      status.message = `${job.tool} failed: ${job.error}`;
      return textReply(status);
    }
    status.message =
      job.state === "failed"
        ? `${job.tool} finished and reported failure; its reply follows.`
        : `${job.tool} finished; its reply follows.`;
    if (!includeResult || !job.reply) return textReply(status);
    // The tool's own content blocks (JSON text, images) follow the status, so
    // nothing is lost by running the call in the background.
    return {
      content: [{ type: "text", text: JSON.stringify(status, null, 2) }, ...job.reply.content],
    };
  }

  /** job_list entries (no results). */
  summaries(): Array<Record<string, unknown>> {
    return this.list().map((job) => ({
      jobId: job.jobId,
      kind: "call",
      tool: job.tool,
      state: job.state,
      deferred: job.deferred,
      elapsedSeconds:
        Math.round((((job.finishedAt ?? Date.now()) - job.startedAt) / 1000) * 10) / 10,
    }));
  }

  private finish(job: BackgroundJob): void {
    job.finishedAt = Date.now();
    for (const done of this.waiters.get(job.jobId) ?? []) done();
    this.waiters.delete(job.jobId);
    // Keep the newest finished jobs; a running job is never dropped.
    const finished = this.list().filter((j) => j.state !== "running");
    for (const old of finished.slice(0, Math.max(0, finished.length - this.keepFinished))) {
      this.jobs.delete(old.jobId);
    }
  }
}

export interface BackgroundPolicy {
  /** Defer a call still running after this many ms; 0 never defers. */
  deferAfterMs: number;
}

/** Sentence appended to the description of a tool that gains `async`. */
const ASYNC_HINT =
  " Long-running (can take minutes): pass async:true to get a jobId at once, then call " +
  "job_status with waitSeconds until it finishes; the result is this tool's normal reply.";

/** For a long tool with its own `async` (autoroute): the parameter says what it does. */
const OWN_ASYNC_HINT =
  " Long-running (can take minutes): pass async:true to get a jobId at once, then follow it " +
  "with job_status (waitSeconds).";

/**
 * Description of the `async` parameter this module adds. The marker phrase
 * "job_status returns this tool's reply" identifies it (the Python schema
 * generator drops the parameter, which only the TypeScript bridge serves).
 */
export const ASYNC_PARAM_DESCRIPTION =
  "Run in the background: return {jobId} at once; job_status returns this tool's reply when " +
  "done (recommended: this tool can take minutes)";

function isZodShape(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const entries = Object.values(value);
  return (
    entries.length === 0 ||
    entries.every((v) => typeof (v as { safeParse?: unknown } | null)?.safeParse === "function")
  );
}

/**
 * Wrap `server.tool` / `server.registerTool` so every tool registered
 * afterwards supports background calls. Install it after
 * flagBackendFailuresAsErrors (so the started reply is not flagged) and
 * before any tool is registered.
 */
export function enableBackgroundCalls(
  server: McpServer,
  calls: BackgroundCalls,
  policy: BackgroundPolicy,
): void {
  const target = server as unknown as Record<string, unknown>;
  for (const method of ["tool", "registerTool"]) {
    const original = target[method];
    if (typeof original !== "function") continue;
    target[method] = (...args: unknown[]) => {
      const name = args[0];
      const last = args.length - 1;
      const handler = args[last];
      if (typeof name !== "string" || typeof handler !== "function" || NEVER_BACKGROUND.has(name)) {
        return (original as (...a: unknown[]) => unknown).apply(server, args);
      }

      // Give long tools an `async` parameter, unless they already have one.
      let addsAsync = false;
      if (isLongRunningTool(name)) {
        if (method === "tool") {
          const shapeIndex = args.findIndex((a, i) => i > 0 && i < last && isZodShape(a));
          if (shapeIndex > 0) {
            const shape = args[shapeIndex] as Record<string, unknown>;
            if (!("async" in shape)) {
              args[shapeIndex] = {
                ...shape,
                async: z.boolean().optional().describe(ASYNC_PARAM_DESCRIPTION),
              };
              addsAsync = true;
            }
          }
          if (typeof args[1] === "string") {
            args[1] = args[1] + (addsAsync ? ASYNC_HINT : OWN_ASYNC_HINT);
          }
        } else if (typeof args[1] === "object" && args[1] !== null) {
          const config = args[1] as { description?: string; inputSchema?: unknown };
          if (isZodShape(config.inputSchema) && !("async" in config.inputSchema)) {
            config.inputSchema = {
              ...config.inputSchema,
              async: z.boolean().optional().describe(ASYNC_PARAM_DESCRIPTION),
            };
            addsAsync = true;
          }
          if (typeof config.description === "string") {
            config.description += addsAsync ? ASYNC_HINT : OWN_ASYNC_HINT;
          }
        }
      }

      args[last] = async (...callArgs: unknown[]) => {
        let toolArgs = callArgs[0];
        let background = false;
        if (addsAsync && typeof toolArgs === "object" && toolArgs !== null && "async" in toolArgs) {
          const { async: wanted, ...rest } = toolArgs as Record<string, unknown>;
          background = wanted === true;
          toolArgs = rest;
        }
        const run = () =>
          Promise.resolve(
            (handler as (...a: unknown[]) => unknown)(toolArgs, ...callArgs.slice(1)),
          );
        if (background) {
          return calls.startedReply(calls.track(name, run(), false));
        }
        if (!policy.deferAfterMs) return run();

        const work = run();
        let timer: NodeJS.Timeout | undefined;
        const DEFERRED = Symbol("deferred");
        const tick = new Promise<typeof DEFERRED>((resolve) => {
          timer = setTimeout(() => resolve(DEFERRED), policy.deferAfterMs);
        });
        try {
          const first = await Promise.race([work, tick]);
          if (first !== DEFERRED) return first;
        } finally {
          clearTimeout(timer);
        }
        return calls.startedReply(calls.track(name, work, true), policy.deferAfterMs);
      };
      return (original as (...a: unknown[]) => unknown).apply(server, args);
    };
  }
}
