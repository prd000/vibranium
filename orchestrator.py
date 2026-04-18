"""Asyncio orchestrator that runs executor, evaluator, and fix agents in a concurrent pipeline."""
import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from vibranium.config import VibraniumConfig
from vibranium.file_locks import FileLockManager
from vibranium.models import PlanItem, ProjectProgress
from vibranium.state import load_progress, validate_and_reconcile

logger = logging.getLogger(__name__)


class Orchestrator:
    """Asyncio orchestrator: runs executor, evaluator, and fix-agent pipelines."""

    def __init__(
        self,
        plan: list[PlanItem],
        config: VibraniumConfig,
        state_dir: Path,
        ws: Any,
        sequential: bool = False,
    ) -> None:
        """Store args; initialize all runtime state; load+reconcile progress; ensure dirs; wire ws."""
        self.plan = plan
        self.config = config
        self.state_dir = state_dir
        self.ws = ws
        self.sequential = sequential
        self.eval_queue: asyncio.Queue = asyncio.Queue()
        self.manager: FileLockManager = FileLockManager()
        self.fix_semaphore: asyncio.Semaphore = asyncio.Semaphore(config.limits.max_concurrent_fix_agents)
        self.active_fix_tasks: set[asyncio.Task] = set()
        self.shutdown_requested: bool = False
        self.cost_limit_hit: bool = False
        self.start_time: float = time.monotonic()

        # Startup sequence
        progress = load_progress(state_dir)
        self.progress: ProjectProgress = validate_and_reconcile(progress, plan)
        (state_dir / "item_log").mkdir(parents=True, exist_ok=True)
        if ws is not None:
            ws.set_state_getter(lambda: self.progress.model_dump(mode="json"))
