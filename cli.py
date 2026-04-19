import argparse
import asyncio
import shutil
import signal
import sys
from pathlib import Path

from vibranium.agent_runner import run_refactor_analyzer
from vibranium.config import VibraniumConfig, find_config, load_config
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
    """Run refactor analyzer, write refactor_plan.md, run Orchestrator in sequential mode; sys.exit with exit code."""
    # Step 1-2: Resolve config
    if args.config is not None:
        config = load_config(Path(args.config))
    else:
        config_path = find_config(Path.cwd())
        config = load_config(config_path) if config_path is not None else VibraniumConfig()
    state_dir = Path(config.paths.state_dir)

    # Step 3: Run analyzer
    refactor_text = asyncio.run(run_refactor_analyzer(args.scope, config))

    # Step 4-5: Write plan to state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    refactor_plan_path = state_dir / "refactor_plan.md"
    refactor_plan_path.write_text(refactor_text, encoding="utf-8")

    # Step 6: Parse plan
    plan = parse_plan(refactor_plan_path)

    # Step 7-8: Instantiate and start WebSocket server
    ws = WsServer(port=args.port)
    asyncio.run(ws.start())

    # Step 9: Instantiate orchestrator with sequential=True
    orchestrator = Orchestrator(plan=plan, config=config, state_dir=state_dir, ws=ws, sequential=True)

    # Step 10: Register SIGINT handler
    signal.signal(signal.SIGINT, lambda *_: orchestrator.request_shutdown())

    # Step 11: Run and exit
    sys.exit(asyncio.run(orchestrator.run()))


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

    args = parser.parse_args()
    if not hasattr(args, "func"):
        # No subcommand given — return silently (preserves test_entry_point_runs)
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
