"""Token and cost tracking for all agent invocations across a pipeline run."""
from vibranium.models import ProjectProgress

RATES: dict[str, dict[str, float]] = {
    "claude-opus-4":     {"input": 15.00, "output": 75.00},
    "claude-opus-3-7":   {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4":   {"input": 3.00,  "output": 15.00},
    "claude-haiku-3-5":  {"input": 0.80,  "output": 4.00},
    "claude-haiku-3":    {"input": 0.25,  "output": 1.25},
    "opusplan":          {"input": 15.00, "output": 75.00},
    "sonnet":            {"input": 3.00,  "output": 15.00},
    "opus":              {"input": 15.00, "output": 75.00},
}


def extract_cost(message: object) -> float:
    """Return USD cost for one SDK message, or 0.0 if usage data is absent."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0.0

    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return 0.0

    model = getattr(message, "model", None)
    if model is None:
        return 0.0

    # Longest-prefix match
    best_key = None
    best_len = -1
    for key in RATES:
        if model.startswith(key) and len(key) > best_len:
            best_key = key
            best_len = len(key)

    if best_key is None:
        return 0.0

    rates = RATES[best_key]
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


def format_cost_table(progress: ProjectProgress) -> str:
    """Return a fixed-width ASCII cost table string for all items in progress."""
    # Column widths: Item=10, each USD column=10; separator=" | "
    item_w = 10
    val_w = 10
    sep = " | "

    header = (
        f"{'Item':<{item_w}}{sep}"
        f"{'Executor':>{val_w}}{sep}"
        f"{'Evaluator':>{val_w}}{sep}"
        f"{'Fix':>{val_w}}{sep}"
        f"{'Total':>{val_w}}"
    )
    # Separator replaces " | " with "-+-" to keep same total width as header
    sep_line = sep.replace(" ", "-").replace("|", "+")  # " | " → "-+-"
    separator = "-" * item_w + sep_line + "-" * val_w + sep_line + "-" * val_w + sep_line + "-" * val_w + sep_line + "-" * val_w

    def item_row(label: str, executor: float, evaluator: float, fix: float, total: float) -> str:
        return (
            f"{label:<{item_w}}{sep}"
            f"{executor:>{val_w}.4f}{sep}"
            f"{evaluator:>{val_w}.4f}{sep}"
            f"{fix:>{val_w}.4f}{sep}"
            f"{total:>{val_w}.4f}"
        )

    # Sort items numerically
    sorted_ids = sorted(
        progress.items.keys(),
        key=lambda item_id: tuple(int(p) for p in item_id.split(".")),
    )

    rows = []
    total_executor = 0.0
    total_evaluator = 0.0
    total_fix = 0.0
    total_total = 0.0

    for item_id in sorted_ids:
        item = progress.items[item_id]
        rows.append(item_row(
            item_id,
            item.executor_cost_usd,
            item.evaluator_cost_usd,
            item.fix_cost_usd,
            item.total_cost_usd,
        ))
        total_executor += item.executor_cost_usd
        total_evaluator += item.evaluator_cost_usd
        total_fix += item.fix_cost_usd
        total_total += item.total_cost_usd

    grand_total_row = item_row("TOTAL", total_executor, total_evaluator, total_fix, total_total)

    lines = [header, separator] + rows + [separator, grand_total_row]
    return "\n".join(lines)
