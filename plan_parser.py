"""Parser for PDH plan.md files that extracts plan items and their state."""
import logging
import os
import re
import tempfile
from pathlib import Path

from vibranium.models import PlanItem

logger = logging.getLogger(__name__)

_ITEM_RE = re.compile(r'^- \[([ x])\] \*\*(\d+\.\d+)\*\* (.+)')
_SPEC_POINTER_RE = re.compile(r'\s*\[spec\]\([^)]+\).*$')
_FILES_AFFECTED_RE = re.compile(r'^\s+-\s+files_affected:\s*(.+)')
_ACCEPTANCE_CRITERIA_RE = re.compile(r'^\s+-\s+acceptance_criteria:\s*(.+)')


def parse_plan(path: Path, include_complete: bool = False) -> list[PlanItem]:
    """Parse a PDH plan.md and return PlanItem objects in file order, skipping checked items unless include_complete=True."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # First pass: identify item lines and their spans of following indented lines
    items: list[PlanItem] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        item_match = _ITEM_RE.match(line)
        if item_match:
            status_char = item_match.group(1)
            item_id = item_match.group(2)
            raw_description = item_match.group(3)

            # Strip spec pointer and everything after it
            description = _SPEC_POINTER_RE.sub('', raw_description).rstrip()

            # Determine segment from ID
            segment = int(item_id.split('.')[0])

            # Collect indented sub-block lines
            files_affected: list[str] = []
            acceptance_criteria: list[str] = []

            j = i + 1
            while j < len(lines):
                sub_line = lines[j]
                # Indented lines that are not item lines belong to the current item
                if sub_line and sub_line[0] == ' ':
                    if _ITEM_RE.match(sub_line):
                        # An indented item line is still an item — stop collecting
                        break
                    files_match = _FILES_AFFECTED_RE.match(sub_line)
                    if files_match:
                        files_affected = [v.strip() for v in files_match.group(1).split(',')]
                        j += 1
                        continue
                    ac_match = _ACCEPTANCE_CRITERIA_RE.match(sub_line)
                    if ac_match:
                        acceptance_criteria = [v.strip() for v in ac_match.group(1).split(',')]
                        j += 1
                        continue
                    # Unrecognised indented line — silently ignore
                    j += 1
                    continue
                else:
                    # Non-indented line ends the sub-block
                    break
            i = j

            # Decide whether to include this item
            is_complete = (status_char == 'x')
            if is_complete and not include_complete:
                # Warn if sub-blocks are missing (even for skipped items? spec says
                # "when parse_plan is called" — warning applies to all parsed items)
                # The spec says warn when either field is absent; apply after parsing.
                # We still check and warn for completeness even for skipped items.
                if not files_affected:
                    logger.warning("Item %s has no files_affected", item_id)
                if not acceptance_criteria:
                    logger.warning("Item %s has no acceptance_criteria", item_id)
                continue

            # Log warnings for missing sub-blocks
            if not files_affected:
                logger.warning("Item %s has no files_affected", item_id)
            if not acceptance_criteria:
                logger.warning("Item %s has no acceptance_criteria", item_id)

            items.append(PlanItem(
                id=item_id,
                segment=segment,
                description=description,
                files_affected=files_affected,
                acceptance_criteria=acceptance_criteria,
                dependencies=[],
            ))
        else:
            i += 1

    return items


def update_item_checkbox(path: Path, item_id: str, state: str = "x") -> None:
    """Find item_id's checkbox line, replace its single bracket character with state, write back atomically."""
    content = path.read_text(encoding="utf-8")

    pattern = r'- \[[ x]\] \*\*' + re.escape(item_id) + r'\*\*'
    if not re.search(pattern, content):
        raise ValueError(f"Item {item_id!r} not found in {path}")

    replacement_pattern = r'(- \[)[ x](\] \*\*' + re.escape(item_id) + r'\*\*)'
    new_content = re.sub(replacement_pattern, r'\g<1>' + state + r'\g<2>', content)

    dir_ = path.parent
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        dir=dir_,
        delete=False,
        suffix='.tmp',
    ) as fh:
        fh.write(new_content)
        tmp_path_str = fh.name
    os.replace(tmp_path_str, path)
