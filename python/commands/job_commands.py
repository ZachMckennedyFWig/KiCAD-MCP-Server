"""MCP tools over the background-job registry (``utils/jobs.py``).

``job_status``, ``job_list`` and ``job_cancel`` only read or signal job state.
``job_apply`` runs a finished job's completion step (for ``autoroute``: write
the routed copy over the board file and reload it) on the main command thread,
so it never races another board mutation.
"""

from typing import Any, Dict

from utils.jobs import FINISHED_STATES, JOBS


class JobCommands:
    """Handlers for job_status / job_list / job_cancel / job_apply."""

    def job_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job_id = params.get("jobId")
        if not job_id:
            return {"success": False, "message": "jobId is required"}
        job = JOBS.get(str(job_id))
        if job is None:
            return {
                "success": False,
                "message": f"Unknown job {job_id}",
                "errorDetails": (
                    "Jobs live in the Python worker's memory; a worker restart forgets them. "
                    "Use job_list to see known jobs."
                ),
            }
        try:
            log_lines = int(params.get("logLines", 20))
        except (TypeError, ValueError):
            log_lines = 20
        include_result = bool(params.get("includeResult", True))
        return {"success": True, **job.snapshot(max(0, log_lines), include_result)}

    def job_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        states = params.get("states")
        if isinstance(states, str):
            states = [states]
        jobs = JOBS.list(kind=params.get("kind"), states=states)
        return {
            "success": True,
            "count": len(jobs),
            "jobs": [job.snapshot(log_lines=0, include_result=False) for job in jobs],
        }

    def job_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job_id = params.get("jobId")
        if not job_id:
            return {"success": False, "message": "jobId is required"}
        job = JOBS.get(str(job_id))
        if job is None:
            return {"success": False, "message": f"Unknown job {job_id}"}
        if job.state in FINISHED_STATES:
            return {
                "success": True,
                "message": f"Job {job_id} already finished ({job.state}); nothing to cancel",
                "state": job.state,
            }
        JOBS.cancel(str(job_id))
        JOBS.wait(str(job_id), timeout=15)
        return {
            "success": True,
            "message": f"Cancel requested for {job_id}",
            "state": job.state,
            "note": "Cancelling never changes the board: results are only written by job_apply.",
        }

    def job_apply(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job_id = params.get("jobId")
        if not job_id:
            return {"success": False, "message": "jobId is required"}
        # Options understood by apply steps (autoroute: importAnyway overrides the
        # importMode=if_better rejection, nothing else).
        options = {"importAnyway": bool(params.get("importAnyway", False))}
        return JOBS.apply(str(job_id), options)
