"""Runner that dispatches executor, evaluator, and fix subagents via the Claude Agent SDK."""
import asyncio
import logging
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher, TextBlock, ThinkingBlock, ToolUseBlock
except ImportError:  # pragma: no cover
    query = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment]
    HookMatcher = None  # type: ignore[assignment]
    TextBlock = None  # type: ignore[assignment]
    ThinkingBlock = None  # type: ignore[assignment]
    ToolUseBlock = None  # type: ignore[assignment]

from vibranium.config import VibraniumConfig, effective_tests_dir
from vibranium.cost_tracker import extract_cost
from vibranium.file_locks import FileLockManager, make_pretooluse_hook, make_test_guard_hook
from vibranium.models import PlanItem, Verdict, Issue, Severity
from vibranium.prompts import (
    build_executor_prompt, build_evaluator_prompt, build_fix_executor_prompt,
    build_refactor_prompt, build_refactor_executor_prompt, build_refactor_evaluator_prompt,
)
from vibranium.state import read_item_log

logger = logging.getLogger(__name__)


def _print_msg(label: str, msg: Any) -> None:
    """Print tool calls, thinking, and text blocks from an SDK message to stdout."""
    for block in getattr(msg, "content", []):
        if ToolUseBlock is not None and isinstance(block, ToolUseBlock):
            parts = []
            for k, v in block.input.items():
                s = str(v)
                if len(s) > 80:
                    s = s[:77] + "..."
                parts.append(f"{k}={s!r}")
            print(f"  [{label}] > {block.name}({', '.join(parts)})", flush=True)
        elif ThinkingBlock is not None and isinstance(block, ThinkingBlock):
            preview = block.thinking[:200].replace("\n", " ")
            if len(block.thinking) > 200:
                preview += "..."
            print(f"  [{label}] ~ {preview}", flush=True)


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
    t_dir = effective_tests_dir(config, Path(config.paths.project_root))
    if config.paths.hide_tests_from_executor and t_dir is not None:
        guard_fn = make_test_guard_hook(t_dir)
        hooks["PreToolUse"].append(HookMatcher(matcher="Read|Glob|Grep|Bash", hooks=[guard_fn]))

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
        _print_msg(item.id, msg)

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
    t_dir = effective_tests_dir(config, Path(config.paths.project_root))
    prompt: str = build_evaluator_prompt(item, item_log, tests_dir=t_dir)

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
        _print_msg(item.id, msg)
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
    t_dir = effective_tests_dir(config, Path(config.paths.project_root))
    if config.paths.hide_tests_from_executor and t_dir is not None:
        guard_fn = make_test_guard_hook(t_dir)
        hooks["PreToolUse"].append(HookMatcher(matcher="Read|Glob|Grep|Bash", hooks=[guard_fn]))

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


async def run_refactor_executor(
    item: PlanItem,
    config: VibraniumConfig,
    manager: FileLockManager,
    state_dir: Path,
    dep_summaries: dict[str, str],
) -> float:
    """Run a refactor executor subagent for item; return total USD cost incurred."""
    # Step 1: Read item log
    item_log: str = await asyncio.to_thread(read_item_log, item.id, state_dir)

    # Step 2: Build prompt
    prompt: str = build_refactor_executor_prompt(item, dep_summaries, item_log)

    # Step 3: Build hook list — write guard + unconditional test guard
    hook_fn = make_pretooluse_hook(manager, item.id)
    hooks = {"PreToolUse": [HookMatcher(matcher="Write|Edit|MultiEdit", hooks=[hook_fn])]}
    t_dir = effective_tests_dir(config, Path(config.paths.project_root))
    if t_dir is not None:
        guard_fn = make_test_guard_hook(t_dir)
        hooks["PreToolUse"].append(HookMatcher(matcher="Read|Glob|Grep|Bash", hooks=[guard_fn]))

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
        _print_msg(item.id, msg)

    # Step 6: Return total cost
    return total_cost


async def run_refactor_evaluator(
    item: PlanItem,
    config: VibraniumConfig,
    state_dir: Path,
) -> tuple[Verdict, float]:
    """Run a refactor evaluator subagent for item; return (verdict, total_cost_usd)."""
    # Step 1: Read item log
    item_log: str = await asyncio.to_thread(read_item_log, item.id, state_dir)

    # Step 2: Build prompt — pass tests_dir so evaluator knows where to run tests
    t_dir = effective_tests_dir(config, Path(config.paths.project_root))
    prompt: str = build_refactor_evaluator_prompt(item, item_log, tests_dir=t_dir)

    # Step 3: Build options — no test guard; evaluator has full test access
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
        _print_msg(item.id, msg)
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


async def run_spec_agent(
    prompt: str,
    config: VibraniumConfig,
    cwd: Path,
) -> str:
    """Run a spec-writing agent; return concatenated assistant text."""
    options = ClaudeAgentOptions(
        model=config.models.executor,
        cwd=str(cwd),
        allowed_tools=["Write", "Read"],
        permission_mode="acceptEdits",
        max_turns=30,
    )
    full_response: str = ""
    async for msg in query(prompt=prompt, options=options):
        for block in getattr(msg, "content", []):
            if isinstance(block, TextBlock):
                full_response += block.text
    return full_response


async def run_init_agent(
    prompt: str,
    config: VibraniumConfig,
    cwd: Path,
) -> str:
    """Run a plan-decomposition agent; return concatenated assistant text."""
    options = ClaudeAgentOptions(
        model=config.models.executor,
        cwd=str(cwd),
        allowed_tools=["Write", "Read"],
        permission_mode="acceptEdits",
        max_turns=40,
    )
    full_response: str = ""
    async for msg in query(prompt=prompt, options=options):
        for block in getattr(msg, "content", []):
            if isinstance(block, TextBlock):
                full_response += block.text
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
