/**
 * Background job tools (MCP_ISSUES principle 6, #5m).
 *
 * Long tools (autoroute with async:true, and any tool built on
 * python/utils/jobs.py) return a jobId at once instead of holding the command
 * channel past its timeout. These tools work for every job kind.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { formatKicadResult } from "./tool-response.js";

export function registerJobTools(server: McpServer, callKicadScript: Function) {
  server.tool(
    "job_status",
    "State of a background job started by a long tool (e.g. autoroute with async:true). Returns {jobId, kind, state: queued|running|succeeded|failed|cancelled, progress 0..1, message, elapsedSeconds, secondsSinceProgress, stalled (no progress for the job's stallTimeout), logTail:[last lines], result (when finished), applyStep:{required, applied}}. Never changes the board.",
    {
      jobId: z.string().describe("Job id returned by the tool that started it"),
      logLines: z.number().optional().describe("Log lines to include (default 20, 0 for none)"),
      includeResult: z
        .boolean()
        .optional()
        .describe("Include the finished job's result (default true)"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("job_status", args)),
  );

  server.tool(
    "job_list",
    "List background jobs known to this server session (newest last), without results or logs. Returns {count, jobs:[{jobId, kind, state, progress, message, elapsedSeconds, stalled, ...}]}. Jobs are kept in memory; a server restart forgets them.",
    {
      kind: z.string().optional().describe("Only jobs of this kind, e.g. autoroute"),
      states: z
        .array(z.string())
        .optional()
        .describe("Only jobs in these states (queued, running, succeeded, failed, cancelled)"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("job_list", args)),
  );

  server.tool(
    "job_cancel",
    "Cancel a running background job (kills its child processes). Cancelling never changes the board: job results are only written by job_apply. Returns {state}.",
    {
      jobId: z.string().describe("Job id"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("job_cancel", args)),
  );

  server.tool(
    "job_apply",
    "Write a finished job's result into the board (for autoroute: replace the .kicad_pcb with the routed and evaluated copy, then reload it). Runs on the main command thread, so it never races another edit. Refuses when the board changed in memory or on disk since the job started, or when the job's importMode=if_better check rejected the result (pass importAnyway:true to override that check only). Returns {success, boardPath, kicad:{before, after}, decision}.",
    {
      jobId: z.string().describe("Job id of a succeeded job"),
      importAnyway: z
        .boolean()
        .optional()
        .describe("Apply even though the if_better evaluation rejected the result"),
    },
    async (args: any) => formatKicadResult(await callKicadScript("job_apply", args)),
  );
}
