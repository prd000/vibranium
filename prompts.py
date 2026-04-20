"""Prompt templates and builders for executor, evaluator, and fix agents."""
from pathlib import Path

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

## Tests Location
{tests_location}

## Item Log
{item_log}

## Feedback Rules
When writing Issues in your verdict JSON:
- Reference failures by acceptance criterion number ("Criterion N is not satisfied").
- Describe the failure behaviorally: what the code does vs. what the spec requires.
- Set Issue.file to the SOURCE file containing the bug, never a test file.
- Do NOT include: test function names, paths inside the test directory, raw assertion
  values, or pytest output verbatim.

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

_SPEC_TEMPLATE: str = """\
## Your Role
You are a technical product manager. Your job is to produce a complete, implementation-ready
spec document that a software agent can implement autonomously without further questions.

## Project Description
{description}

## Spec Requirements
Write a spec covering all 9 sections. Every section must be specific and complete.
Mark any technical decision not provided by the user as "implementor's choice: [your choice]".

Sections:
1. Vision \u2014 2-3 sentences on what is built and why
2. Users \u2014 table of user types and their goals
3. Capabilities \u2014 feature areas with specific, testable items
4. Technical Design \u2014 stack, data model, API design, integrations, project structure
5. Interface \u2014 key screens, commands, or endpoints
6. Constraints \u2014 non-functional requirements
7. Edge Cases and Error Handling \u2014 per capability
8. Out of Scope \u2014 explicit exclusion list
9. Acceptance Criteria \u2014 binary pass/fail per capability

## Spec Template
{spec_template}

## Instructions
1. Fill every section with concrete content derived from the description.
2. Acceptance criteria must be binary and testable by an automated agent.
3. Do not ask clarifying questions. Make reasonable choices for anything ambiguous.
4. Use the Write tool to write the finished spec to: {output_path}
5. After writing, print exactly: "Spec written to {output_path}"
"""

_INIT_TEMPLATE: str = """\
## Your Role
You are a project planner decomposing a software specification into a PDH implementation plan.

## Spec Content
{spec_content}

## Decomposition Rules
1. Group related work into segments (feature areas or technical layers). Name each segment.
2. Within each segment, list items ordered by dependency. Each item must be independently
   implementable and evaluatable. Aim for ~1-3 hours of implementation effort per item.
3. Assign IDs: N.M (N = segment number, M = item position within segment, both 1-indexed).
   IDs never change once assigned.
4. Per item, include files_affected and acceptance_criteria as described below.

## CRITICAL: Plan File Format

The plan parser uses these exact regexes to read sub-blocks:
  _FILES_AFFECTED_RE    matches: ^\\s+-\\s+files_affected:\\s*(.+)
  _ACCEPTANCE_CRITERIA_RE matches: ^\\s+-\\s+acceptance_criteria:\\s*(.+)

Both fields MUST be written on a single line, comma-separated. Multi-line bullet lists are
silently ignored, leaving PlanItem.acceptance_criteria = [] and ruining executor quality.

CORRECT format (use this exactly):
  - [ ] **N.M** Description of the work item
    - files_affected: path/to/file.py, path/to/other.py
    - acceptance_criteria: Criterion one passes, Criterion two passes, Criterion three holds

WRONG format (do NOT use):
  - [ ] **N.M** Description
    - files_affected: path/to/file.py
    - acceptance_criteria:
      - Criterion one   <-- THIS IS IGNORED BY THE PARSER

## Output Files
Write all three files using the Write tool.

### {plan_path}
```
# Work Plan

**Source:** {spec_path}
**Generated:** {generated_date}
**Last evaluated:** --

## Segment 1: [Name]
- [ ] **1.1** Description of the first work item
  - files_affected: path/to/file.py, path/to/other.py
  - acceptance_criteria: Criterion one passes, Criterion two passes

[... remaining segments and items ...]

## Evaluator Notes
```

### {project_md_path}
```
# Project State

## What This Project Is
[1-3 sentences from spec vision]

## Spec Reference
{spec_path}

## Current Status
Initialized. No implementation work done yet.

## Architecture Decisions
[Key decisions from Technical Design section]

## Active Work
None yet.

## Known Issues
None yet.

## What's Next
Segment 1: [name] \u2014 item 1.1 [description]

## Do Not
```

### {log_path}
```
# Session Log

## Session: {generated_date}
**Mode:** interactive
**Goal:** Project initialization
**Work Done:**
- Spec read and analyzed
- Plan decomposed into [N] segments, [M] items
**Handoff Notes:**
Ready for implementation. Start with item 1.1.

---
```

After writing all three files, print: "Initialized: [N] segments, [M] items written to {state_dir}/"
Do not ask for confirmation. Write all files now.
"""

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
  - acceptance_criteria: Preserve all public function signatures, net LOC does not increase, no new external imports added

Use sequential IDs starting from 1.1. Do not include segments or spec pointers."""

_REFACTOR_EXECUTOR_TEMPLATE: str = """\
## Refactor Task
{task}

## Constraints
- Preserve all public function/class/method signatures exactly.
- Do not add new external imports (removing unused imports is allowed).
- Goal: reduce complexity — fewer lines, flatter nesting, or eliminated duplication.
- Do not change logic. When in doubt, leave it alone.

## Files to Refactor
{files_affected}

## Acceptance Criteria
{acceptance_criteria}\
{previous_attempts}"""

_REFACTOR_EVALUATOR_TEMPLATE: str = """\
## Your Role
You are an adversarial refactor evaluator. Your job is to confirm that the refactor preserved
all existing behavior exactly, and that the code is genuinely simpler afterward.
Apply stricter criteria than a normal implementation review.

## Refactor Task
{task}

## Files Affected
{files_affected}

## Acceptance Criteria
{acceptance_criteria}

## Tests Location
{tests_location}

## Item Log
{item_log}

## Evaluation Steps
1. Run the test suite — ANY test failure is an automatic FAIL. Refactors may not break tests.
2. Run `git diff HEAD -- <file>` for each affected file to read exactly what changed.
3. For every changed function/method: confirm the new logic is semantically equivalent to
   the original for all reachable paths.
4. Verify all public signatures (names, parameters, return types) are unchanged.
5. Verify no new imports were added (removals are fine).
6. Verify net LOC in affected files did not increase.
7. Flag as FAIL: renamed variables that shadow outer scope, changed default argument values,
   altered exception types/messages, off-by-one errors in rewritten loops.

## Feedback Rules
When writing Issues in your verdict JSON:
- Reference failures by acceptance criterion number ("Criterion N is not satisfied").
- Describe the failure behaviorally: what the code does vs. what the spec requires.
- Set Issue.file to the SOURCE file containing the bug, never a test file.
- Do NOT include: test function names, paths inside the test directory, raw assertion
  values, or pytest output verbatim.

## Output Format
Your response MUST end with a JSON block in exactly this format:

Pass:
{{"verdict": "pass", "notes": "..."}}

Fail:
{{"verdict": "fail", "issues": [{{"file": "...", "line": N, "description": "...", "severity": "critical|major|minor"}}]}}

Severity rules: any critical or major issue = automatic fail; three or more minor issues = fail.
Do not include any text after the closing brace of the JSON block."""


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
    tests_dir: "Path | None" = None,
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

    # Tests location block
    if tests_dir is not None:
        tests_location = f"Run the test suite from: {tests_dir}"
    else:
        tests_location = "Run the existing test suite from the project root."

    # Item log — always present; "None" when empty
    item_log_body = item_log if item_log else "None"

    return _EVALUATOR_TEMPLATE.format(
        task=item.description,
        acceptance_criteria=ac_lines,
        files_affected=files_lines,
        tests_location=tests_location,
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


def build_spec_prompt(
    description: str,
    output_path: str,
    spec_template: str,
) -> str:
    """Return the full prompt string for a spec-writing agent."""
    fallback = "(No description provided \u2014 write a general-purpose template with [TODO] placeholders.)"
    return _SPEC_TEMPLATE.format(
        description=description if description else fallback,
        output_path=output_path,
        spec_template=spec_template,
    )


def build_init_prompt(
    spec_content: str,
    spec_path: str,
    state_dir: str,
    plan_path: str,
    project_md_path: str,
    log_path: str,
    generated_date: str,
) -> str:
    """Return the full prompt string for a plan-decomposition agent."""
    return _INIT_TEMPLATE.format(
        spec_content=spec_content,
        spec_path=spec_path,
        state_dir=state_dir,
        plan_path=plan_path,
        project_md_path=project_md_path,
        log_path=log_path,
        generated_date=generated_date,
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


def build_refactor_executor_prompt(
    item: PlanItem,
    dep_summaries: dict[str, str],
    item_log: str,
) -> str:
    """Return the full prompt string for a refactor executor subagent working on item."""
    # Files affected block
    if item.files_affected:
        files_lines = "\n".join(f"- {f}" for f in item.files_affected)
    else:
        files_lines = "None"

    # Acceptance criteria block
    if item.acceptance_criteria:
        ac_lines = "\n".join(
            f"{i + 1}. {criterion}"
            for i, criterion in enumerate(item.acceptance_criteria)
        )
    else:
        ac_lines = "None"

    # Previous attempts block — omitted entirely when item_log is empty
    if item_log:
        previous_attempts = f"\n\n## Previous Attempts\n{item_log}"
    else:
        previous_attempts = ""

    return _REFACTOR_EXECUTOR_TEMPLATE.format(
        task=item.description,
        files_affected=files_lines,
        acceptance_criteria=ac_lines,
        previous_attempts=previous_attempts,
    )


def build_refactor_evaluator_prompt(
    item: PlanItem,
    item_log: str,
    tests_dir: "Path | None" = None,
) -> str:
    """Return the full prompt string for a refactor evaluator subagent reviewing item."""
    # Files affected block
    if item.files_affected:
        files_lines = "\n".join(f"- {f}" for f in item.files_affected)
    else:
        files_lines = "None"

    # Acceptance criteria block
    if item.acceptance_criteria:
        ac_lines = "\n".join(
            f"{i + 1}. {criterion}"
            for i, criterion in enumerate(item.acceptance_criteria)
        )
    else:
        ac_lines = "None"

    # Tests location block
    if tests_dir is not None:
        tests_location = f"Run the test suite from: {tests_dir}"
    else:
        tests_location = "Run the existing test suite from the project root."

    # Item log — always present; "None" when empty
    item_log_body = item_log if item_log else "None"

    return _REFACTOR_EVALUATOR_TEMPLATE.format(
        task=item.description,
        files_affected=files_lines,
        acceptance_criteria=ac_lines,
        tests_location=tests_location,
        item_log=item_log_body,
    )


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
