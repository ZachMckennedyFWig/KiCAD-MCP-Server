/**
 * Background job tools (MCP_ISSUES principle 6, #5m).
 *
 * Two kinds of job answer to these tools:
 * - Python jobs (`<kind>-<hex>`, python/utils/jobs.py), e.g. autoroute with
 *   async:true, whose result is written by job_apply;
 * - background calls (`bg-<hex>`, src/background-calls.ts): any long tool
 *   called with async:true, or a call the HTTP transport deferred. Their result
 *   is the tool's own reply; there is nothing to apply.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import {
  BACKGROUND_JOB_PREFIX,
  BackgroundCalls,
  MAX_STATUS_WAIT_SECONDS,
} from "../background-calls.js";
import { formatKicadResult } from "./tool-response.js";

const FINISHED = new Set(["succeeded", "failed", "cancelled"]);
const PYTHON_POLL_MS = 2_000;

function textReply(payload: Record<string, unknown>, isError = false) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
    ...(isError ? { isError: true } : {}),
  };
}

export function registerJobTools(
  server: McpServer,
  callKicadScript: Function,
  calls: BackgroundCalls = new BackgroundCalls(),
) {
  server.tool(
    "job_status",
    "State of a background job: one started by a long tool called with async:true, a call that " +
      "was still running when the server answered with a jobId, or autoroute's own async job. " +
      `Pass waitSeconds (up to ${MAX_STATUS_WAIT_SECONDS}) to wait for it to finish before ` +
      "answering, instead of polling in a tight loop. For a background call (jobId bg-...) the " +
      "reply is {jobId, tool, state: running|succeeded|failed, elapsedSeconds, message} followed " +
      "by the tool's own reply once finished. For autoroute jobs: {jobId, kind, state: " +
      "queued|running|succeeded|failed|cancelled, progress 0..1, message, elapsedSeconds, " +
      "secondsSinceProgress, stalled, logTail, result, applyStep:{required, applied}}. Never " +
      "changes the board.",
    {
      jobId: z.string().describe("Job id returned by the tool that started it"),
      waitSeconds: z
        .number()
        .min(0)
        .max(MAX_STATUS_WAIT_SECONDS)
        .optional()
        .describe(
          `Wait up to this many seconds (max ${MAX_STATUS_WAIT_SECONDS}) for the job to finish ` +
            "before answering (default 0: answer at once)",
        ),
      logLines: z.number().optional().describe("Log lines to include (default 20, 0 for none)"),
      includeResult: z
        .boolean()
        .optional()
        .describe("Include the finished job's result (default true)"),
    },
    async (args: any) => {
      const waitMs =
        Math.min(Math.max(Number(args.waitSeconds) || 0, 0), MAX_STATUS_WAIT_SECONDS) * 1000;
      if (String(args.jobId).startsWith(BACKGROUND_JOB_PREFIX)) {
        await calls.wait(args.jobId, waitMs);
        const job = calls.get(args.jobId);
        if (!job) {
          return textReply(
            {
              success: false,
              message: `Unknown job ${args.jobId}`,
              errorDetails:
                "Background calls are kept in memory (the newest 200 finished ones); a server " +
                "restart forgets them.",
            },
            true,
          );
        }
        return calls.statusReply(job, args.includeResult !== false);
      }
      // A Python job: poll its status until it finishes or the wait runs out.
      const pythonArgs = { ...args };
      delete pythonArgs.waitSeconds;
      const deadline = Date.now() + waitMs;
      let result = await callKicadScript("job_status", pythonArgs);
      while (
        waitMs > 0 &&
        result?.success !== false &&
        !FINISHED.has(String(result?.state)) &&
        Date.now() + PYTHON_POLL_MS <= deadline
      ) {
        await new Promise((resolve) => setTimeout(resolve, PYTHON_POLL_MS));
        result = await callKicadScript("job_status", pythonArgs);
      }
      return formatKicadResult(result);
    },
  );

  server.tool(
    "job_list",
    "List background jobs known to this server process (newest last), without results or logs: " +
      "background calls (jobId bg-...) and autoroute jobs. Returns {count, jobs:[{jobId, kind, " +
      "tool?, state, elapsedSeconds, ...}]}. Jobs are kept in memory; a server restart forgets them.",
    {
      kind: z.string().optional().describe("Only jobs of this kind: call, or e.g. autoroute"),
      states: z
        .array(z.string())
        .optional()
        .describe("Only jobs in these states (queued, running, succeeded, failed, cancelled)"),
    },
    async (args: any) => {
      const states: string[] | undefined = args.states;
      let jobs = calls
        .summaries()
        .filter(
          (j) =>
            (!args.kind || args.kind === "call") && (!states || states.includes(String(j.state))),
        );
      const warnings: string[] = [];
      let pythonJobs: any[] = [];
      if (args.kind === "call") {
        // background calls only
      } else if (calls.running() > 0) {
        // The worker is busy with (or queued behind) a background call; asking
        // it would wait for that call to finish.
        warnings.push(
          "Autoroute jobs are not listed while a background call holds the KiCad worker; " +
            "call job_list again when it has finished.",
        );
      } else {
        const result = await callKicadScript("job_list", args);
        if (result?.success === false) return formatKicadResult(result);
        pythonJobs = Array.isArray(result?.jobs) ? result.jobs : [];
      }
      jobs = [...pythonJobs, ...jobs];
      return textReply({
        success: true,
        count: jobs.length,
        jobs,
        ...(warnings.length ? { warnings } : {}),
      });
    },
  );

  server.tool(
    "job_cancel",
    "Cancel a running autoroute job (kills its child processes). Cancelling never changes the " +
      "board: job results are only written by job_apply. Background calls (jobId bg-...) cannot " +
      "be cancelled: the KiCad command runs to the end so no file is left half-written. Returns " +
      "{state}.",
    {
      jobId: z.string().describe("Job id"),
    },
    async (args: any) => {
      if (String(args.jobId).startsWith(BACKGROUND_JOB_PREFIX)) {
        const job = calls.get(args.jobId);
        return textReply(
          {
            success: false,
            message: job
              ? `${job.tool} (${args.jobId}) cannot be cancelled; it is ${job.state}`
              : `Unknown job ${args.jobId}`,
            errorDetails:
              "A background call is a KiCad command the worker runs to completion; interrupting " +
              "it could leave a half-written file. Wait for it with job_status.",
          },
          true,
        );
      }
      return formatKicadResult(await callKicadScript("job_cancel", args));
    },
  );

  server.tool(
    "job_apply",
    "Write a finished autoroute job's result into the board (replace the .kicad_pcb with the routed and evaluated copy, then reload it). Runs on the main command thread, so it never races another edit. Refuses when the board changed in memory or on disk since the job started, or when the job's importMode=if_better check rejected the result (pass importAnyway:true to override that check only). Background calls (jobId bg-...) need no apply: their changes are already made. Returns {success, boardPath, kicad:{before, after}, decision}.",
    {
      jobId: z.string().describe("Job id of a succeeded job"),
      importAnyway: z
        .boolean()
        .optional()
        .describe("Apply even though the if_better evaluation rejected the result"),
    },
    async (args: any) => {
      if (String(args.jobId).startsWith(BACKGROUND_JOB_PREFIX)) {
        return textReply(
          {
            success: false,
            message: `${args.jobId} is a background call; there is nothing to apply`,
            errorDetails:
              "A background call makes its changes itself, like the same call without async. " +
              "Read its result with job_status.",
          },
          true,
        );
      }
      return formatKicadResult(await callKicadScript("job_apply", args));
    },
  );
}
