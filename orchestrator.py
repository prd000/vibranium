"""Asyncio orchestrator that runs executor, evaluator, and fix agents in a concurrent pipeline."""
import asyncio
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vibranium.agent_runner import (
    run_executor, run_evaluator, run_fix_executor, with_retry,
    run_refactor_executor, run_refactor_evaluator,
)
from vibranium.config import VibraniumConfig
from vibranium.file_locks import FileLockManager
from vibranium.models import EvalResult, FlaggedItem, Issue, ItemStatus, PlanItem, ProjectProgress, Verdict
from vibranium.plan_parser import update_item_checkbox
from vibranium.state import append_deferred_work, async_save_progress, load_progress, validate_and_reconcile

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
        refactor_mode: bool = False,
    ) -> None:
        """Store args; initialize all runtime state; load+reconcile progress; ensure dirs; wire ws."""
        self.plan = plan
        self.config = config
        self.state_dir = state_dir
        self.ws = ws
        self.sequential = sequential
        self.refactor_mode = refactor_mode
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

    async def run(self) -> int:
        """Run both pipelines concurrently, drain fix tasks, print summary, return exit code."""
        await asyncio.gather(self._execute_pipeline(), self._evaluate_pipeline())

        if self.active_fix_tasks:
            await asyncio.gather(*self.active_fix_tasks, return_exceptions=True)

        self._print_summary()

        if self.cost_limit_hit:
            return 3
        if len(self.progress.flagged_for_review) > 0:
            return 1
        return 0

    async def _execute_pipeline(self) -> None:
        """Iterate self.plan; skip complete-pass and flagged items; run executor for each; feed eval_queue; send None sentinel when done."""
        for item in self.plan:
            ip = self.progress.items[item.id]

            # Skip items already complete with pass or previously flagged
            if ip.status == ItemStatus.COMPLETE and ip.eval == EvalResult.PASS:
                continue
            if ip.status == ItemStatus.FLAGGED:
                continue

            # Executor already ran but evaluator didn't finish — re-queue for evaluation
            if ip.status == ItemStatus.COMPLETE:
                await self.eval_queue.put(item)
                continue

            # Check shutdown and cost limit before spawning
            if self.shutdown_requested:
                break
            if self.progress.totals.total_cost_usd >= self.config.limits.max_total_cost_usd:
                self.cost_limit_hit = True
                break

            # Mark item in_progress and save
            ip.status = ItemStatus.IN_PROGRESS
            await async_save_progress(self.progress, self.state_dir)
            print(f"[{item.id}] executor starting: {item.description[:70]}", flush=True)

            # Emit item_status WebSocket event
            if self.ws is not None:
                asyncio.create_task(self.ws.broadcast({"type": "item_status", "item_id": item.id, "status": "in_progress"}))

            # Acquire file locks, run executor in try/finally
            await self.manager.acquire(item.files_affected, item.id)
            try:
                _runner = run_refactor_executor if self.refactor_mode else run_executor
                cost = await with_retry(
                    lambda: _runner(item, self.config, self.manager, self.state_dir, {}),
                    label=item.id,
                )
                # Success path
                ip.status = ItemStatus.COMPLETE
                ip.eval = None  # evaluator will set this
                ip.executor_cost_usd += cost
                ip.total_cost_usd = ip.executor_cost_usd + ip.evaluator_cost_usd + ip.fix_cost_usd
                self.progress.totals.total_cost_usd += cost
                self.progress.totals.total_executor_calls += 1
                await async_save_progress(self.progress, self.state_dir)
                print(f"[{item.id}] executor done (${cost:.4f})", flush=True)
                await self.eval_queue.put(item)
            except Exception as exc:
                print(f"[{item.id}] executor error: {exc}", flush=True)
                await self._defer_item(item, "agent_error")
            finally:
                self.manager.release(item.files_affected, item.id)

        # Always send sentinel to signal _evaluate_pipeline to stop
        await self.eval_queue.put(None)

    async def _evaluate_pipeline(self) -> None:
        """Consume eval_queue; evaluate each item; route verdict to pass path or fix_loop task."""
        while True:
            item = await self.eval_queue.get()

            if item is None or self.shutdown_requested:
                self.eval_queue.task_done()
                break

            print(f"[{item.id}] evaluating...", flush=True)
            _eval_fn = run_refactor_evaluator if self.refactor_mode else run_evaluator
            verdict, eval_cost = await _eval_fn(item, self.config, self.state_dir)

            ip = self.progress.items[item.id]
            ip.evaluator_cost_usd += eval_cost
            ip.total_cost_usd = ip.executor_cost_usd + ip.evaluator_cost_usd + ip.fix_cost_usd
            self.progress.totals.total_cost_usd += eval_cost
            self.progress.totals.total_evaluator_calls += 1

            if verdict.passed:
                ip.eval = EvalResult.PASS
                await async_save_progress(self.progress, self.state_dir)
                await asyncio.to_thread(update_item_checkbox, Path(self.config.paths.plan), item.id, "x")
                print(f"[{item.id}] PASS", flush=True)
                if self.ws is not None:
                    asyncio.create_task(self.ws.broadcast({
                        "type": "eval_result",
                        "item_id": item.id,
                        "passed": True,
                        "issues": [],
                    }))
            else:
                ip.eval = EvalResult.FAIL
                await async_save_progress(self.progress, self.state_dir)
                issue_summary = "; ".join(i.description for i in verdict.issues[:2])
                print(f"[{item.id}] FAIL — {issue_summary}", flush=True)
                if self.ws is not None:
                    asyncio.create_task(self.ws.broadcast({
                        "type": "eval_result",
                        "item_id": item.id,
                        "passed": False,
                        "issues": [iss.model_dump() for iss in verdict.issues],
                    }))
                task = asyncio.create_task(self._fix_loop(item, verdict))
                self.active_fix_tasks.add(task)
                task.add_done_callback(self.active_fix_tasks.discard)

            self.eval_queue.task_done()

            if self.sequential:
                await self.eval_queue.join()

    async def _fix_loop(self, item: PlanItem, verdict: Verdict) -> None:
        """Run up to config.limits.max_fix_attempts fix+re-eval cycles; defer item if all fail."""
        async with self.fix_semaphore:
            for attempt in range(self.config.limits.max_fix_attempts):
                print(f"[{item.id}] fix attempt {attempt + 1}/{self.config.limits.max_fix_attempts}", flush=True)
                # 1. Emit fix_attempt WS event
                if self.ws is not None:
                    asyncio.create_task(self.ws.broadcast({
                        "type": "fix_attempt",
                        "item_id": item.id,
                        "attempt": attempt,
                        "max_attempts": self.config.limits.max_fix_attempts,
                    }))

                # 2. Acquire file locks
                await self.manager.acquire(item.files_affected, item.id)
                try:
                    # 3. Run fix executor with retry; accumulate cost
                    fix_cost = await with_retry(
                        lambda: run_fix_executor(item, verdict, self.config, self.manager, self.state_dir),
                        label=item.id,
                    )
                    ip = self.progress.items[item.id]
                    ip.fix_cost_usd += fix_cost
                    ip.total_cost_usd = ip.executor_cost_usd + ip.evaluator_cost_usd + ip.fix_cost_usd
                    self.progress.totals.total_cost_usd += fix_cost
                    self.progress.totals.total_fix_calls += 1
                    ip.fix_attempts += 1
                    await async_save_progress(self.progress, self.state_dir)
                finally:
                    # 4. Always release file locks
                    self.manager.release(item.files_affected, item.id)

                # 5. Re-evaluate (outside the lock — evaluator is read-only)
                _eval_fn = run_refactor_evaluator if self.refactor_mode else run_evaluator
                verdict, eval_cost = await _eval_fn(item, self.config, self.state_dir)
                ip = self.progress.items[item.id]
                ip.evaluator_cost_usd += eval_cost
                ip.total_cost_usd = ip.executor_cost_usd + ip.evaluator_cost_usd + ip.fix_cost_usd
                self.progress.totals.total_cost_usd += eval_cost
                self.progress.totals.total_evaluator_calls += 1
                await async_save_progress(self.progress, self.state_dir)

                # 6. Pass path: update state, checkbox, WS, return immediately
                if verdict.passed:
                    ip.eval = EvalResult.PASS
                    await async_save_progress(self.progress, self.state_dir)
                    await asyncio.to_thread(
                        update_item_checkbox, Path(self.config.paths.plan), item.id, "x"
                    )
                    print(f"[{item.id}] PASS after fix", flush=True)
                    if self.ws is not None:
                        asyncio.create_task(self.ws.broadcast({
                            "type": "eval_result",
                            "item_id": item.id,
                            "passed": True,
                            "issues": [],
                        }))
                    return

        # 7. Loop exhausted without passing
        await self._defer_item(item, "eval_exhausted", verdict.issues)

    async def _defer_item(
        self,
        item: PlanItem,
        reason: str,
        issues: list[Issue] | None = None,
    ) -> None:
        """Flag item in progress, append to deferred_work.md, emit flagged+cost_update WS events."""
        resolved_issues = issues or []

        # 1. Mutate item progress
        ip = self.progress.items[item.id]
        ip.status = ItemStatus.FLAGGED
        print(f"[{item.id}] FLAGGED ({reason})", flush=True)

        # 2. Append FlaggedItem to flagged_for_review
        now = datetime.now(timezone.utc)
        self.progress.flagged_for_review.append(
            FlaggedItem(item_id=item.id, reason=reason, last_issues=resolved_issues, timestamp=now)
        )

        # 3. Increment totals counter
        self.progress.totals.items_flagged += 1

        # 4. Save progress atomically
        await async_save_progress(self.progress, self.state_dir)

        # 5. Git-restore files on refactor failure so codebase is never left worse
        if self.refactor_mode and item.files_affected:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "checkout", "HEAD", "--"] + item.files_affected,
                cwd=self.config.paths.project_root,
                check=False,
            )

        # 6. Write deferred_work.md entry (blocking I/O via to_thread)
        await asyncio.to_thread(append_deferred_work, item, reason, resolved_issues, self.state_dir)

        # 7. Emit WS events (guarded)
        if self.ws is not None:
            asyncio.create_task(self.ws.broadcast({
                "type": "flagged",
                "item_id": item.id,
                "reason": reason,
                "issues": [iss.model_dump() for iss in resolved_issues],
            }))
            asyncio.create_task(self.ws.broadcast({
                "type": "cost_update",
                "total_cost_usd": self.progress.totals.total_cost_usd,
                "limit_usd": self.config.limits.max_total_cost_usd,
            }))

    def request_shutdown(self) -> None:
        """Set shutdown_requested=True and print a shutdown notice to stderr."""
        self.shutdown_requested = True
        print("\nShutdown requested. Waiting for current agents to finish...\n", file=sys.stderr)

    def _print_summary(self) -> None:
        """Print summary table of item counts, total cost, and elapsed time to stdout."""
        sep = "=" * 60
        elapsed = time.monotonic() - self.start_time
        print(sep)
        print("Run Summary")
        print(sep)
        print(f"Complete : {self.progress.totals.items_complete}")
        print(f"Flagged  : {self.progress.totals.items_flagged}")
        print(f"Pending  : {self.progress.totals.items_pending}")
        print(f"Total cost: ${self.progress.totals.total_cost_usd:.4f}")
        print(f"Elapsed  : {elapsed:.1f}s")
        print(sep)
