"""Command routes for the routing-workflow tools and the job tools.

Kept in one place so ``kicad_interface.py`` registers them with a single
``**routing_workflow_routes(lambda: self.board)`` entry. The handlers read the
live board through the provider, so board reloads (open_board, the autoroute
apply step, ...) need no ``_update_command_handlers`` bookkeeping.
"""

from typing import Any, Callable, Dict

from commands.astar_router import RouterCommands
from commands.fanout import FanoutCommands
from commands.job_commands import JobCommands

# Tools here that change the board in memory (auto-saved by the dispatcher).
BOARD_MUTATING = ("fanout_to_planes", "route_connection", "route_bus")


def routing_workflow_routes(board_provider: Callable[[], Any]) -> Dict[str, Callable]:
    router = RouterCommands(board_provider=board_provider)
    fanout = FanoutCommands(board_provider=board_provider)
    jobs = JobCommands()
    return {
        "fanout_to_planes": fanout.fanout_to_planes,
        "route_connection": router.route_connection,
        "route_bus": router.route_bus,
        "length_match": router.length_match,
        "job_status": jobs.job_status,
        "job_list": jobs.job_list,
        "job_cancel": jobs.job_cancel,
        "job_apply": jobs.job_apply,
    }
