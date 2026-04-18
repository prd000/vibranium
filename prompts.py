"""Prompt templates and builders for executor, evaluator, and fix agents."""
from vibranium.models import Issue, PlanItem, Verdict

# ---------------------------------------------------------------------------
# Private template constants
# ---------------------------------------------------------------------------

_EXECUTOR_TEMPLATE: str = """\
## Task
{task}

## Acceptance Criteria
{acceptance_criteria}

## Dependencies
{dependencies}\
{previous_attempts}"""

_EVALUATOR_TEMPLATE: str = """\
## Your Role
You are an adversarial evaluator. Your job is to find real bugs, not to rubber-stamp work.
Assume the implementation is subtly wrong until you prove otherwise. Check every acceptance
criterion individually; inspect every file in the affected list; run the existing test suite.
Look for: unhandled error cases, off-by-one errors, null assumptions, type coercion traps,
race conditions, missing input validation, hardcoded values, broken imports, interface
mismatches, and tautological tests.

## Task
{task}

## Acceptance Criteria
{acceptance_criteria}

## Files Affected
{files_affected}

## Item Log
{item_log}

## Output Format
Your response MUST end with a JSON block in exactly this format:

Pass:
{{"verdict": "pass", "notes": "..."}}

Fail:
{{"verdict": "fail", "issues": [{{"file": "...", "line": N, "description": "...", "severity": "critical|major|minor"}}]}}

Severity rules: any critical or major issue = automatic fail; three or more minor issues = fail.
Do not include any text after the closing brace of the JSON block."""

_FIX_EXECUTOR_TEMPLATE: str = """\
## Original Task
{task}

## Issues to Fix
{issues}

## Full Item Log
{item_log}"""

_REFACTOR_TEMPLATE: str = """\
## Your Role
You are a codebase analyst. Analyze the codebase and produce a refactor plan in PDH item format.

## Instructions
1. Read all source files in the target scope.
2. Identify specific, bounded improvements: remove duplication, clarify abstractions, fix
   naming inconsistencies, add missing error handling, improve testability.
3. Do NOT rewrite working logic unless there is a concrete correctness or maintainability issue.
4. Produce a list of work items, each with a clear description, acceptance criteria, and
   files_affected list.

## Scope
{scope}

## Output Format
Produce a valid PDH plan.md item list. Each item must follow this format exactly:

- [ ] **N.1** Description of the refactor
  - files_affected: path/to/file.py, path/to/other.py
  - acceptance_criteria:
    - Criterion one
    - Criterion two

Use sequential IDs starting from 1.1. Do not include segments or spec pointers."""


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------

def build_executor_prompt(
    item: PlanItem,
    dep_summaries: dict[str, str],
    item_log: str,
) -> str:
    """Return the full prompt string for an executor subagent working on item."""
    # Acceptance criteria block
    if item.acceptance_criteria:
        ac_lines = "\n".join(
            f"{i + 1}. {criterion}"
            for i, criterion in enumerate(item.acceptance_criteria)
        )
    else:
        ac_lines = "None"

    # Dependencies block
    if dep_summaries:
        dep_lines = "\n".join(
            f"### {dep_id}\n{summary}"
            for dep_id, summary in dep_summaries.items()
        )
    else:
        dep_lines = "None"

    # Previous attempts block — omitted entirely when item_log is empty
    if item_log:
        previous_attempts = f"\n\n## Previous Attempts\n{item_log}"
    else:
        previous_attempts = ""

    return _EXECUTOR_TEMPLATE.format(
        task=item.description,
        acceptance_criteria=ac_lines,
        dependencies=dep_lines,
        previous_attempts=previous_attempts,
    )


def build_evaluator_prompt(
    item: PlanItem,
    item_log: str,
) -> str:
    """Return the full prompt string for an evaluator subagent reviewing item."""
    # Acceptance criteria block
    if item.acceptance_criteria:
        ac_lines = "\n".join(
            f"{i + 1}. {criterion}"
            for i, criterion in enumerate(item.acceptance_criteria)
        )
    else:
        ac_lines = "None"

    # Files affected block
    if item.files_affected:
        files_lines = "\n".join(f"- {f}" for f in item.files_affected)
    else:
        files_lines = "None"

    # Item log — always present; "None" when empty
    item_log_body = item_log if item_log else "None"

    return _EVALUATOR_TEMPLATE.format(
        task=item.description,
        acceptance_criteria=ac_lines,
        files_affected=files_lines,
        item_log=item_log_body,
    )


def build_fix_executor_prompt(
    item: PlanItem,
    verdict: Verdict,
    item_log: str,
) -> str:
    """Return the full prompt string for a fix executor subagent addressing verdict issues."""
    # Issues block
    if verdict.issues:
        issue_lines = "\n".join(
            _format_issue(i + 1, issue)
            for i, issue in enumerate(verdict.issues)
        )
    else:
        issue_lines = "None"

    # Full item log — always present; "None" when empty
    item_log_body = item_log if item_log else "None"

    return _FIX_EXECUTOR_TEMPLATE.format(
        task=item.description,
        issues=issue_lines,
        item_log=item_log_body,
    )


def build_refactor_prompt(
    scope: str | None,
) -> str:
    """Return the full prompt string for a refactor analyzer subagent."""
    if scope:
        scope_body = f"Limit your analysis to: {scope}"
    else:
        scope_body = "Analyze the entire codebase."

    return _REFACTOR_TEMPLATE.format(scope=scope_body)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_issue(n: int, issue: Issue) -> str:
    """Format a single issue as a numbered list entry."""
    severity_upper = issue.severity.value.upper()
    if issue.line is not None:
        return f"{n}. [{severity_upper}] {issue.file}:{issue.line} \u2014 {issue.description}"
    else:
        return f"{n}. [{severity_upper}] {issue.file} \u2014 {issue.description}"
