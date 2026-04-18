"""Pydantic models for plan items, agent results, and pipeline state."""
import json
import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ItemStatus(str, Enum):
    """Lifecycle state of a plan item in the pipeline."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FLAGGED = "flagged"


class EvalResult(str, Enum):
    """Outcome of an evaluator pass on a completed item."""

    PASS = "pass"
    FAIL = "fail"


class Severity(str, Enum):
    """Issue severity as reported by the evaluator subagent."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class PlanItem(BaseModel):
    """A single work item parsed from plan.md."""

    id: str                              # e.g. "2.1"
    segment: int                         # N from N.M
    description: str
    files_affected: list[str] = []
    acceptance_criteria: list[str] = []
    dependencies: list[str] = []


class Issue(BaseModel):
    """A single evaluator-identified problem in the implementation."""

    file: str
    line: int | None = None
    description: str
    severity: Severity


class Verdict(BaseModel):
    """Parsed evaluator verdict from a raw LLM response string."""

    passed: bool
    issues: list[Issue] = []
    notes: str = ""

    @classmethod
    def from_json(cls, raw: str) -> "Verdict":
        """Parse the last {...} block in raw, map verdict==pass to passed=True; raise ValueError if no block found."""
        matches = re.findall(r'\{.*?\}', raw, re.DOTALL)
        if not matches:
            raise ValueError("No JSON block found in evaluator response")
        # The non-greedy regex finds innermost braces; reassemble to outermost
        # brace-balanced blocks by scanning raw for top-level { } spans.
        top_level_blocks = []
        depth = 0
        start = None
        for i, ch in enumerate(raw):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    top_level_blocks.append(raw[start:i + 1])
                    start = None
        if not top_level_blocks:
            raise ValueError("No JSON block found in evaluator response")
        data = json.loads(top_level_blocks[-1])
        passed = data.get("verdict") == "pass"
        issues = [Issue(**i) for i in data.get("issues", [])]
        notes = data.get("notes", "")
        return cls(passed=passed, issues=issues, notes=notes)


class ItemProgress(BaseModel):
    """Per-item runtime state tracked in progress.json."""

    status: ItemStatus = ItemStatus.PENDING
    eval: EvalResult | None = None
    fix_attempts: int = 0
    files_affected: list[str] = []
    executor_cost_usd: float = 0.0
    evaluator_cost_usd: float = 0.0
    fix_cost_usd: float = 0.0
    total_cost_usd: float = 0.0


class FlaggedItem(BaseModel):
    """Record of an item written to deferred_work.md."""

    item_id: str
    reason: str
    last_issues: list[Issue] = []
    timestamp: datetime


class ProgressTotals(BaseModel):
    """Aggregate counts and costs across all items."""

    items_complete: int = 0
    items_pending: int = 0
    items_in_progress: int = 0
    items_flagged: int = 0
    total_cost_usd: float = 0.0
    total_executor_calls: int = 0
    total_evaluator_calls: int = 0
    total_fix_calls: int = 0


class ProjectProgress(BaseModel):
    """Root state model serialized to state/progress.json."""

    plan_file: str
    started_at: datetime
    last_updated: datetime
    items: dict[str, ItemProgress] = {}
    flagged_for_review: list[FlaggedItem] = []
    totals: ProgressTotals = Field(default_factory=ProgressTotals)
