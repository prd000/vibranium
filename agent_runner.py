"""Runner that dispatches executor, evaluator, and fix subagents via the Claude Agent SDK."""
import asyncio
import logging
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher, TextBlock
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment]
    HookMatcher = None  # type: ignore[assignment]
    TextBlock = None  # type: ignore[assignment]

from vibranium.config import VibraniumConfig
from vibranium.cost_tracker import extract_cost
from vibranium.file_locks import FileLockManager, make_pretooluse_hook
from vibranium.models import PlanItem, Verdict, Issue, Severity
from vibranium.prompts import build_executor_prompt, build_evaluator_prompt, build_fix_executor_prompt, build_refactor_prompt
from vibranium.state import read_item_log

logger = logging.getLogger(__name__)


async def run_executor(
    item: PlanItem,
    config: VibraniumConfig,
    manager: FileLockManager,
    state_dir: Path,
    dep_summaries: dict[str, str],
) -> float:
    """Run an executor subagent for item; return total USD cost incurred."""
    # Step 1: Read item log
    item_log: str = await asyncio.to_thread(read_item_log, item.id, state_dir)

    # Step 2: Build prompt
    prompt: str = build_executor_prompt(item, dep_summaries, item_log)

    # Step 3: Build hook list
    hook_fn = make_pretooluse_hook(manager, item.id)
    hooks = {"PreToolUse": [HookMatcher(matcher="Write|Edit|MultiEdit", hooks=[hook_fn])]}

    # Step 4: Build options
    options = ClaudeAgentOptions(
        model=config.models.executor,
        cwd=config.paths.project_root,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="acceptEdits",
        max_turns=config.limits.max_turns_per_agent,
        hooks=hooks,
    )

    # Step 5: Stream query and accumulate cost
    total_cost: float = 0.0
    async for msg in query(prompt=prompt, options=options):
        cost = extract_cost(msg)
        total_cost += cost
        if getattr(msg, "tool_use", None):
            logger.info("[%s] executor: %s", item.id, msg.tool_use)

    # Step 6: Return total cost
    return total_cost


async def run_evaluator(
    item: PlanItem,
    config: VibraniumConfig,
    state_dir: Path,
) -> tuple[Verdict, float]:
    """Run an evaluator subagent for item; return (verdict, total_cost_usd)."""
    # Step 1: Read item log
    item_log: str = await asyncio.to_thread(read_item_log, item.id, state_dir)

    # Step 2: Build prompt
    prompt: str = build_evaluator_prompt(item, item_log)

    # Step 3: Build options
    options = ClaudeAgentOptions(
        model=config.models.evaluator,
        cwd=config.paths.project_root,
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="plan",
        max_turns=50,
    )

    # Step 4: Stream query and accumulate
    total_cost: float = 0.0
    full_response: str = ""
    async for msg in query(prompt=prompt, options=options):
        cost = extract_cost(msg)
        total_cost += cost
        for block in getattr(msg, "content", []):
            if isinstance(block, TextBlock):
                full_response += block.text

    # Step 5: Parse verdict
    try:
        verdict = Verdict.from_json(full_response)
    except Exception:
        verdict = Verdict(
            passed=False,
            issues=[Issue(
                file="evaluator",
                line=None,
                description="Evaluator output did not contain a valid JSON verdict block",
                severity=Severity.CRITICAL,
            )],
        )

    # Step 6: Return
    return (verdict, total_cost)


async def run_fix_executor(
    item: PlanItem,
    verdict: Verdict,
    config: VibraniumConfig,
    manager: FileLockManager,
    state_dir: Path,
) -> float:
    """Run a fix executor subagent for item addressing verdict issues; return total USD cost incurred."""
    # Step 1: Read item log
    item_log: str = await asyncio.to_thread(read_item_log, item.id, state_dir)

    # Step 2: Build prompt
    prompt: str = build_fix_executor_prompt(item, verdict, item_log)

    # Step 3: Build hook list
    hook_fn = make_pretooluse_hook(manager, item.id)
    hooks = {"PreToolUse": [HookMatcher(matcher="Write|Edit|MultiEdit", hooks=[hook_fn])]}

    # Step 4: Build options
    options = ClaudeAgentOptions(
        model=config.models.fix_executor,
        cwd=config.paths.project_root,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="acceptEdits",
        max_turns=config.limits.max_turns_per_agent,
        hooks=hooks,
    )

    # Step 5: Stream query and accumulate cost
    total_cost: float = 0.0
    async for msg in query(prompt=prompt, options=options):
        total_cost += extract_cost(msg)
        if getattr(msg, "tool_use", None):
            logger.info("[%s] fix_executor: %s", item.id, msg.tool_use)

    # Step 6: Return total cost
    return total_cost


async def run_refactor_analyzer(
    scope: str | None,
    config: VibraniumConfig,
) -> str:
    """Run a read-only Opus refactor analyzer; return concatenated assistant text."""
    # Step 1: Build prompt
    prompt = build_refactor_prompt(scope)

    # Step 2: Build options
    options = ClaudeAgentOptions(
        model=config.models.refactor_analyzer,
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="plan",
    )

    # Step 3: Stream query and accumulate text
    full_response: str = ""
    async for msg in query(prompt=prompt, options=options):
        for block in getattr(msg, "content", []):
            if isinstance(block, TextBlock):
                full_response += block.text

    # Step 4: Return concatenated text
    return full_response


async def with_retry(
    coro_factory,
    max_attempts: int = 2,
    base_delay: float = 5.0,
    label: str = "",
):
    """Call await coro_factory(); on exception log+sleep+retry; re-raise after max_attempts."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            logger.error("[%s] attempt %d failed: %s", label, attempt, exc)
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc
