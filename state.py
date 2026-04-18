"""Disk-based state management for tracking pipeline progress across restarts."""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from vibranium.models import Issue, ItemProgress, ItemStatus, PlanItem, ProjectProgress

logger = logging.getLogger(__name__)


def validate_and_reconcile(
    progress: ProjectProgress,
    plan_items: list[PlanItem],
) -> ProjectProgress:
    """Reconcile progress against plan_items; mutate and return progress.

    1. For each item in plan_items whose id is absent from progress.items,
       insert ItemProgress(status=ItemStatus.PENDING).
    2. For each item in progress.items whose status is IN_PROGRESS,
       reset status to PENDING and emit logger.warning.
    3. For each id in progress.items that is not in the plan_items id set,
       emit logger.warning (do not remove the entry).
    4. Recompute progress.totals from the final state of progress.items.
    5. Return progress (mutated in place).
    """
    plan_ids = {item.id for item in plan_items}

    # Step 1: Insert missing plan items as PENDING
    for item in plan_items:
        if item.id not in progress.items:
            progress.items[item.id] = ItemProgress(status=ItemStatus.PENDING)

    # Step 2: Reset IN_PROGRESS items to PENDING
    for item_id, item_progress in progress.items.items():
        if item_progress.status == ItemStatus.IN_PROGRESS:
            item_progress.status = ItemStatus.PENDING
            logger.warning("Item %s was in_progress on load; resetting to pending", item_id)

    # Step 3: Warn about orphan progress entries
    for item_id in progress.items:
        if item_id not in plan_ids:
            logger.warning("Progress entry %s has no matching plan item; keeping as-is", item_id)

    # Step 4: Recompute totals (status counts and total_cost_usd only)
    items_complete = 0
    items_pending = 0
    items_in_progress = 0
    items_flagged = 0
    total_cost_usd = 0.0

    for item_progress in progress.items.values():
        if item_progress.status == ItemStatus.COMPLETE:
            items_complete += 1
        elif item_progress.status == ItemStatus.PENDING:
            items_pending += 1
        elif item_progress.status == ItemStatus.IN_PROGRESS:
            items_in_progress += 1
        elif item_progress.status == ItemStatus.FLAGGED:
            items_flagged += 1
        total_cost_usd += item_progress.total_cost_usd

    progress.totals.items_complete = items_complete
    progress.totals.items_pending = items_pending
    progress.totals.items_in_progress = items_in_progress
    progress.totals.items_flagged = items_flagged
    progress.totals.total_cost_usd = total_cost_usd

    return progress


def load_progress(state_dir: Path) -> ProjectProgress:
    """Read progress.json from state_dir; return fresh ProjectProgress if file missing."""
    progress_file = state_dir / "progress.json"
    if not progress_file.exists():
        now = datetime.now(timezone.utc)
        return ProjectProgress(
            plan_file="plan.md",
            started_at=now,
            last_updated=now,
            items={},
            flagged_for_review=[],
        )
    data = json.loads(progress_file.read_text(encoding="utf-8"))
    return ProjectProgress.model_validate(data)


def save_progress(progress: ProjectProgress, state_dir: Path) -> None:
    """Set last_updated=now(UTC), serialize to JSON, write atomically via tmp+os.replace."""
    progress.last_updated = datetime.now(timezone.utc)
    json_content = progress.model_dump_json(indent=2)
    progress_file = state_dir / "progress.json"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=state_dir,
        delete=False,
        suffix=".tmp",
    ) as fh:
        fh.write(json_content)
        tmp_path_str = fh.name
    os.replace(tmp_path_str, progress_file)


async def async_load_progress(state_dir: Path) -> ProjectProgress:
    """Wrap load_progress in asyncio.to_thread for non-blocking async callers."""
    return await asyncio.to_thread(load_progress, state_dir)


async def async_save_progress(progress: ProjectProgress, state_dir: Path) -> None:
    """Wrap save_progress in asyncio.to_thread for non-blocking async callers."""
    await asyncio.to_thread(save_progress, progress, state_dir)


def read_item_log(item_id: str, state_dir: Path) -> str:
    """Read state_dir/item_log/{item_id}.md; return full text or '' if file does not exist."""
    log_file = state_dir / "item_log" / f"{item_id}.md"
    try:
        return log_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def append_item_log(item_id: str, state_dir: Path, content: str) -> None:
    """Append content to state_dir/item_log/{item_id}.md.

    If the file already exists (non-empty), prepend '\\n---\\n\\n' before content.
    If the file does not exist (or is empty/new), write content directly with no separator.
    Creates state_dir/item_log/ directory if it does not exist.
    """
    log_dir = state_dir / "item_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{item_id}.md"
    if log_file.exists() and log_file.stat().st_size > 0:
        separator = "\n---\n\n"
    else:
        separator = ""
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(separator + content)


async def async_read_item_log(item_id: str, state_dir: Path) -> str:
    """Wrap read_item_log in asyncio.to_thread for non-blocking async callers."""
    return await asyncio.to_thread(read_item_log, item_id, state_dir)


async def async_append_item_log(item_id: str, state_dir: Path, content: str) -> None:
    """Wrap append_item_log in asyncio.to_thread for non-blocking async callers."""
    await asyncio.to_thread(append_item_log, item_id, state_dir, content)


def append_deferred_work(
    item: PlanItem,
    reason: str,
    issues: list[Issue],
    state_dir: Path,
) -> None:
    """Append a deferred-work entry to state_dir/deferred_work.md and print a stderr banner."""
    deferred_file = state_dir / "deferred_work.md"

    # Create file with header if it does not exist
    if not deferred_file.exists():
        deferred_file.write_text("# Deferred Work\n\n", encoding="utf-8")

    # Build issue bullet lines
    issue_lines = []
    for issue in issues:
        if issue.line is not None:
            loc = f"{issue.file}:{issue.line}"
        else:
            loc = issue.file
        issue_lines.append(f"- {issue.severity.value.upper()} {loc} — {issue.description}")

    timestamp = datetime.now(timezone.utc).isoformat()

    if issue_lines:
        entry = (
            f"## Item {item.id} — {item.description} — {timestamp}\n"
            f"**Reason:** {reason}\n\n"
            f"### Final Issues\n"
            + "\n".join(issue_lines)
            + "\n\n---\n"
        )
    else:
        entry = (
            f"## Item {item.id} — {item.description} — {timestamp}\n"
            f"**Reason:** {reason}\n\n"
            f"### Final Issues\n\n---\n"
        )

    with deferred_file.open("a", encoding="utf-8") as fh:
        fh.write(entry)

    print(
        f"\n{'='*60}\nDEFERRED: Item {item.id}\nReason: {reason}\nSee: {state_dir}/deferred_work.md\n{'='*60}\n",
        file=sys.stderr,
    )


async def async_append_deferred_work(
    item: PlanItem,
    reason: str,
    issues: list[Issue],
    state_dir: Path,
) -> None:
    """Wrap append_deferred_work in asyncio.to_thread for non-blocking async callers."""
    await asyncio.to_thread(append_deferred_work, item, reason, issues, state_dir)
