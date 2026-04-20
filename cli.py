import argparse
import asyncio
import logging
import shutil
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

from vibranium.agent_runner import run_init_agent, run_refactor_analyzer, run_spec_agent
from vibranium.config import VibraniumConfig, find_config, load_config
from vibranium.prompts import build_init_prompt, build_spec_prompt
from vibranium.cost_tracker import format_cost_table
from vibranium.models import EvalResult, ItemStatus
from vibranium.orchestrator import Orchestrator
from vibranium.plan_parser import parse_plan
from vibranium.state import load_progress
from vibranium.ws_server import WsServer


def cmd_run(args: argparse.Namespace) -> None:
    """Load config, parse plan, start WsServer, run Orchestrator; warn and exit 2 if progress.json exists without --fresh; clear state if --fresh."""
    # Step 1-2: Resolve config
    if args.config is not None:
        config_path = Path(args.config)
        config = load_config(config_path)
    else:
        config_path = find_config(Path.cwd())
        if config_path is not None:
            config = load_config(config_path)
        else:
            config = VibraniumConfig()

    # Step 3: Resolve paths
    plan_path = Path(args.plan)
    if not plan_path.exists():
        fallback = Path(".state") / "plan.md"
        if fallback.exists():
            plan_path = fallback
    config.paths.plan = str(plan_path)
    state_dir = Path(config.paths.state_dir)

    # Step 4: Parse plan
    plan = parse_plan(plan_path)

    # Step 5: Check progress.json
    progress_file = state_dir / "progress.json"
    if progress_file.exists() and not args.fresh:
        print(
            "Warning: state/progress.json already exists. Use --fresh to clear existing state before running.",
            file=sys.stderr,
        )
        sys.exit(2)
    if progress_file.exists() and args.fresh:
        shutil.rmtree(state_dir)

    # Step 6: Ensure state_dir exists
    state_dir.mkdir(parents=True, exist_ok=True)

    # Step 7-8: Instantiate and start WebSocket server
    ws = WsServer(port=args.port)
    asyncio.run(ws.start())

    # Step 10: Instantiate orchestrator
    orchestrator = Orchestrator(plan=plan, config=config, state_dir=state_dir, ws=ws)

    # Step 11: Register SIGINT handler
    signal.signal(signal.SIGINT, lambda *_: orchestrator.request_shutdown())

    # Step 12: Run and exit
    sys.exit(asyncio.run(orchestrator.run()))


def cmd_resume(args: argparse.Namespace) -> None:
    """Load config, parse plan, start WsServer, run Orchestrator; exit 2 if progress.json does not exist."""
    # Step 1-2: Resolve config
    if args.config is not None:
        config_path = Path(args.config)
        config = load_config(config_path)
    else:
        config_path = find_config(Path.cwd())
        if config_path is not None:
            config = load_config(config_path)
        else:
            config = VibraniumConfig()

    # Step 3: Resolve paths
    plan_path = Path(args.plan)
    if not plan_path.exists():
        fallback = Path(".state") / "plan.md"
        if fallback.exists():
            plan_path = fallback
    config.paths.plan = str(plan_path)
    state_dir = Path(config.paths.state_dir)

    # Step 4: Parse plan
    plan = parse_plan(plan_path)

    # Step 5: Check progress.json
    progress_file = state_dir / "progress.json"
    if not progress_file.exists():
        print(
            "Error: state/progress.json not found. Run 'vibranium run' first.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 6: Ensure state_dir exists
    state_dir.mkdir(parents=True, exist_ok=True)

    # Step 7-8: Instantiate and start WebSocket server
    ws = WsServer(port=args.port)
    asyncio.run(ws.start())

    # Step 10: Instantiate orchestrator
    orchestrator = Orchestrator(plan=plan, config=config, state_dir=state_dir, ws=ws)

    # Step 11: Register SIGINT handler
    signal.signal(signal.SIGINT, lambda *_: orchestrator.request_shutdown())

    # Step 12: Run and exit
    sys.exit(asyncio.run(orchestrator.run()))


def cmd_status(args: argparse.Namespace) -> None:
    """Load progress.json, print status table to stdout; exit 1 if not found."""
    # Resolve config and state_dir identically to cmd_run/cmd_resume
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()
    state_dir = Path(config.paths.state_dir)

    # Check for progress.json
    progress_file = state_dir / "progress.json"
    if not progress_file.exists():
        print(
            "Error: state/progress.json not found. Run 'vibranium run' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load progress
    progress = load_progress(state_dir)

    # Status symbol mapping
    _status_sym = {
        ItemStatus.COMPLETE: "\u2713",
        ItemStatus.IN_PROGRESS: "\u23f3",
        ItemStatus.FLAGGED: "\u2691",
        ItemStatus.PENDING: "-",
    }

    # Eval symbol mapping
    _eval_sym = {
        EvalResult.PASS: "\u2713",
        EvalResult.FAIL: "\u2717",
        None: "-",
    }

    # Column widths
    item_w, status_w, eval_w, fixes_w, cost_w = 10, 8, 8, 6, 12
    sep = " | "

    # Header
    header = (
        f"{'Item ID':<{item_w}}{sep}"
        f"{'Status':^{status_w}}{sep}"
        f"{'Eval':^{eval_w}}{sep}"
        f"{'Fixes':>{fixes_w}}{sep}"
        f"{'Cost (USD)':>{cost_w}}"
    )

    # Separator
    col_sep = "-+-"
    separator = (
        "-" * item_w + col_sep
        + "-" * status_w + col_sep
        + "-" * eval_w + col_sep
        + "-" * fixes_w + col_sep
        + "-" * cost_w
    )

    print(header)
    print(separator)

    # Sort items numerically by segment.item
    def _sort_key(item_id: str) -> tuple:
        return tuple(int(p) for p in item_id.split("."))

    for item_id in sorted(progress.items.keys(), key=_sort_key):
        item = progress.items[item_id]
        status_sym = _status_sym.get(item.status, "-")
        eval_sym = _eval_sym.get(item.eval, "-")
        row = (
            f"{item_id:<{item_w}}{sep}"
            f"{status_sym:^{status_w}}{sep}"
            f"{eval_sym:^{eval_w}}{sep}"
            f"{item.fix_attempts:>{fixes_w}}{sep}"
            f"{item.total_cost_usd:>{cost_w}.4f}"
        )
        print(row)

    print(separator)

    # Totals row
    total_fixes = sum(ip.fix_attempts for ip in progress.items.values())
    totals_row = (
        f"{'TOTAL':<{item_w}}{sep}"
        f"{' ':^{status_w}}{sep}"
        f"{' ':^{eval_w}}{sep}"
        f"{total_fixes:>{fixes_w}}{sep}"
        f"{progress.totals.total_cost_usd:>{cost_w}.4f}"
    )
    print(totals_row)

    # Deferred footer
    if len(progress.flagged_for_review) > 0:
        count = len(progress.flagged_for_review)
        print()
        print(f"{count} deferred item(s) \u2014 see {str(state_dir)}/deferred_work.md")


def cmd_cost(args: argparse.Namespace) -> None:
    """Load progress.json, print format_cost_table result to stdout; exit 1 if not found."""
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()
    state_dir = Path(config.paths.state_dir)

    progress_file = state_dir / "progress.json"
    if not progress_file.exists():
        print(
            "Error: state/progress.json not found. Run 'vibranium run' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    progress = load_progress(state_dir)
    print(format_cost_table(progress))


def cmd_refactor(args: argparse.Namespace) -> None:
    """Run refactor analyzer, write refactor_plan.md, run Orchestrator in refactor+sequential mode; sys.exit with exit code."""
    # Step 1-2: Resolve config
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()
    state_dir = Path(config.paths.state_dir)

    # Step 3: Capture git baseline commit hash for post-run diff reference
    git_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.paths.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    baseline_commit = git_result.stdout.strip() if git_result.returncode == 0 else None

    # Step 4: Run analyzer
    refactor_text = asyncio.run(run_refactor_analyzer(args.scope, config))

    # Step 5-6: Write plan to state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    refactor_plan_path = state_dir / "refactor_plan.md"
    refactor_plan_path.write_text(refactor_text, encoding="utf-8")

    # Step 7: Parse plan
    plan = parse_plan(refactor_plan_path)

    # Step 8-9: Instantiate and start WebSocket server
    ws = WsServer(port=args.port)
    asyncio.run(ws.start())

    # Step 10: Instantiate orchestrator with sequential=True and refactor_mode=True
    orchestrator = Orchestrator(
        plan=plan, config=config, state_dir=state_dir, ws=ws,
        sequential=True, refactor_mode=True,
    )

    # Step 11: Register SIGINT handler
    signal.signal(signal.SIGINT, lambda *_: orchestrator.request_shutdown())

    # Step 12: Run orchestrator
    exit_code = asyncio.run(orchestrator.run())

    # Step 13: Print post-run summary with baseline reference
    if baseline_commit:
        short_hash = baseline_commit[:8]
        print(f"Refactor complete. Baseline: {short_hash}. Run 'git diff {short_hash}' to review all changes.")

    sys.exit(exit_code)


def cmd_reset(args: argparse.Namespace) -> None:
    """Prompt for confirmation then delete state directory; warn and exit 1 if in_progress items exist without --force."""
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()
    state_dir = Path(config.paths.state_dir)

    if args.force:
        shutil.rmtree(state_dir)
        print("State cleared.")
        return

    progress_file = state_dir / "progress.json"
    if progress_file.exists():
        progress = load_progress(state_dir)
        in_progress_ids = [
            item_id
            for item_id, item in progress.items.items()
            if item.status == ItemStatus.IN_PROGRESS
        ]
        if in_progress_ids:
            print(
                f"Warning: the following items are in_progress: {', '.join(in_progress_ids)}. "
                "Use --force to reset anyway.",
                file=sys.stderr,
            )
            sys.exit(1)

    response = input("Delete ./state/ directory? [y/N] ")
    if response.strip().lower() == "y":
        shutil.rmtree(state_dir)
        print("State cleared.")


def cmd_spec(args: argparse.Namespace) -> None:
    """Run spec-writing agent; write spec.md (or --output path) to cwd."""
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()

    output_path = Path(args.output).resolve()

    # Load spec template from repo root (graceful degradation if not found)
    spec_template_path = Path(__file__).parent.parent / "spec-template.md"
    spec_template = spec_template_path.read_text(encoding="utf-8") if spec_template_path.exists() else ""

    prompt = build_spec_prompt(
        description=args.description or "",
        output_path=str(output_path),
        spec_template=spec_template,
    )

    print(f"Writing spec to {output_path} ...")
    asyncio.run(run_spec_agent(prompt, config, Path.cwd()))

    if output_path.exists():
        print(f"Done. Review {output_path} and run 'vibranium init' when ready.")
    else:
        print(
            f"Warning: agent did not create {output_path}. Check output above.",
            file=sys.stderr,
        )


def cmd_init(args: argparse.Namespace) -> None:
    """Read spec, decompose into PDH plan, create .state/ directory, git commit."""
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"Error: spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    spec_content = spec_path.read_text(encoding="utf-8")

    # PDH state dir is always .state/ (separate from vibranium's runtime ./state/)
    state_dir = Path.cwd() / ".state"
    plan_path = state_dir / "plan.md"
    project_md_path = state_dir / "project.md"
    log_path = state_dir / "log.md"

    if project_md_path.exists():
        response = input(".state/project.md already exists. Overwrite? [y/N] ")
        if response.strip().lower() != "y":
            print("Aborted.")
            return

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "specs").mkdir(parents=True, exist_ok=True)

    prompt = build_init_prompt(
        spec_content=spec_content,
        spec_path=str(spec_path),
        state_dir=str(state_dir),
        plan_path=str(plan_path),
        project_md_path=str(project_md_path),
        log_path=str(log_path),
        generated_date=date.today().isoformat(),
    )

    print(f"Decomposing {spec_path} into plan ...")
    asyncio.run(run_init_agent(prompt, config, Path.cwd()))

    missing = [p for p in [plan_path, project_md_path, log_path] if not p.exists()]
    if missing:
        print(
            f"Warning: agent did not create: {', '.join(str(p) for p in missing)}",
            file=sys.stderr,
        )
        return

    try:
        subprocess.run(["git", "add", str(state_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "pdh: initialize project state"],
            check=True,
            capture_output=True,
        )
        print("Committed: pdh: initialize project state")
    except subprocess.CalledProcessError as e:
        print(
            f"Warning: git commit failed: {e.stderr.decode(errors='replace').strip()}",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print("Warning: git not found; skipping commit.", file=sys.stderr)


def cmd_config(args: argparse.Namespace) -> None:
    """Interactive wizard to create or update vibranium_config.yaml in cwd."""
    config_file = Path.cwd() / "vibranium_config.yaml"
    if config_file.exists():
        existing = load_config(config_file)
        print(f"Updating existing config: {config_file}")
    else:
        existing = VibraniumConfig()
        print(f"Creating new config: {config_file}")

    print("Press Enter to keep the current value shown in [brackets].\n")

    def _prompt(label: str, current: object) -> str:
        val = input(f"{label} [{current}]: ").strip()
        return val if val else str(current)

    def _prompt_int(label: str, current: int) -> int:
        while True:
            val = input(f"{label} [{current}]: ").strip()
            if not val:
                return current
            try:
                return int(val)
            except ValueError:
                print("  Please enter an integer.")

    print("--- Models ---")
    executor_model = _prompt("Executor model", existing.models.executor)
    evaluator_model = _prompt("Evaluator model", existing.models.evaluator)
    fix_executor_model = _prompt("Fix executor model", existing.models.fix_executor)
    refactor_model = _prompt("Refactor analyzer model", existing.models.refactor_analyzer)

    print("\n--- Limits ---")
    max_fix_attempts = _prompt_int("Max fix attempts", existing.limits.max_fix_attempts)
    max_concurrent = _prompt_int(
        "Max concurrent fix agents", existing.limits.max_concurrent_fix_agents
    )

    print("\n--- Paths ---")
    plan_path_val = _prompt("Plan path", existing.paths.plan)
    state_dir_val = _prompt("State directory", existing.paths.state_dir)

    print("\n--- UI ---")
    ws_port = _prompt_int("WebSocket port", existing.ui.websocket_port)

    config_data = {
        "models": {
            "executor": executor_model,
            "evaluator": evaluator_model,
            "fix_executor": fix_executor_model,
            "refactor_analyzer": refactor_model,
        },
        "limits": {
            "max_fix_attempts": max_fix_attempts,
            "max_concurrent_fix_agents": max_concurrent,
        },
        "paths": {
            "plan": plan_path_val,
            "state_dir": state_dir_val,
        },
        "ui": {
            "websocket_port": ws_port,
        },
    }

    with config_file.open("w", encoding="utf-8") as fh:
        yaml.dump(config_data, fh, default_flow_style=False, sort_keys=False)

    print(f"\nConfig written to {config_file}")


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate cmd_* handler."""
    parser = argparse.ArgumentParser("vibranium")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="Override config file path")
    subparsers = parser.add_subparsers(dest="command")

    run_p = subparsers.add_parser("run", help="Start execution from plan.md")
    run_p.add_argument("--plan", default="./plan.md", help="Path to plan.md")
    run_p.add_argument("--fresh", action="store_true",
                       help="Clear existing state before running")
    run_p.add_argument("--port", type=int, default=8765,
                       help="WebSocket port")
    run_p.set_defaults(func=cmd_run)

    resume_p = subparsers.add_parser("resume", help="Resume from existing progress.json")
    resume_p.add_argument("--plan", default="./plan.md", help="Path to plan.md")
    resume_p.add_argument("--port", type=int, default=8765,
                          help="WebSocket port")
    resume_p.set_defaults(func=cmd_resume)

    status_p = subparsers.add_parser("status", help="Print item states and costs")
    status_p.set_defaults(func=cmd_status)

    cost_p = subparsers.add_parser("cost", help="Print per-item cost breakdown")
    cost_p.set_defaults(func=cmd_cost)

    refactor_p = subparsers.add_parser("refactor", help="Run refactor analysis")
    refactor_p.add_argument("--scope", default=None,
                            help="Limit refactor to a specific path or module")
    refactor_p.add_argument("--port", type=int, default=8765, help="WebSocket port")
    refactor_p.set_defaults(func=cmd_refactor)

    reset_p = subparsers.add_parser("reset", help="Delete state directory after confirmation")
    reset_p.add_argument("--force", action="store_true",
                         help="Skip in-progress item check")
    reset_p.set_defaults(func=cmd_reset)

    spec_p = subparsers.add_parser("spec", help="Generate a spec.md via Claude agent")
    spec_p.add_argument(
        "--description", default=None,
        help="Brief description of the project (Claude expands into full spec)",
    )
    spec_p.add_argument(
        "--output", default="spec.md",
        help="Output path for spec file (default: spec.md)",
    )
    spec_p.set_defaults(func=cmd_spec)

    init_p = subparsers.add_parser("init", help="Decompose spec.md into .state/ plan")
    init_p.add_argument(
        "--spec", default="spec.md",
        help="Path to spec file (default: spec.md)",
    )
    init_p.set_defaults(func=cmd_init)

    config_p = subparsers.add_parser(
        "config", help="Interactive wizard to create/update vibranium_config.yaml",
    )
    config_p.set_defaults(func=cmd_config)

    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        # No subcommand given — return silently (preserves test_entry_point_runs)
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
