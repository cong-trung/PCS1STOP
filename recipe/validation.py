"""Thin adapter around ``recipe_checker`` (the Python port of the iLLIAD checker).

``check_recipe_string`` only reports problems as formatted text blocks.  This module runs it on the *edited*
recipe text and additionally maps every block that belongs to an XOFYBIN rule to that rule's ``RuleName`` so
the grid can show a per-row "Validation Error".  Nothing in recipe_checker.py is modified or duplicated.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from recipe import recipe_checker as rc


@dataclass
class CheckResult:
    text: str                     # what the Error tab shows
    ok: bool                      # no errors at all
    n_errors: int = 0
    per_rule: dict[str, list[str]] = field(default_factory=dict)   # XOFYBIN RuleName -> error lines
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


_NODE_TAG = re.compile(r"<\s*([A-Za-z_][\w.\-]*)")
_RULE_NAME = re.compile(r'RuleName="([^"]*)"')


def _split_blocks(errors) -> tuple[dict[str, list[str]], int]:
    """Parse RecipeErrorTracker.accumulated: ' Recipe Node: ...\\n<xml>\\n Errors:\\n     msg\\n     msg'."""
    per_rule: dict[str, list[str]] = {}
    total = 0
    for block in errors.accumulated:
        head, sep, tail = block.partition("\n Errors:\n")
        body = tail if sep else block.partition(":\n")[2]
        msgs = [ln.strip() for ln in body.split("\n") if ln.strip()]
        total += len(msgs)
        tag = _NODE_TAG.search(head) if sep else None
        if tag and tag.group(1).upper() == "XOFYBIN":
            name = _RULE_NAME.search(head)
            if name and name.group(1):  # a rule without a name cannot be matched to its row (the model reports that)
                per_rule.setdefault(name.group(1), []).extend(msgs)
    return per_rule, total


def run_check(xml_text: str, filename: str, alarm_map_path: str = "", skip_workflow_check: bool = True) -> CheckResult:
    """Run recipe_checker on ``xml_text``.  The paths/options come from the caller (config.json), none live here."""
    t0 = time.perf_counter()
    warnings: list[str] = []
    try:
        amap = rc.AlarmIdMap.load(alarm_map_path) if alarm_map_path else None
        if amap is None:
            warnings.append(f"Alarm map not found ({alarm_map_path or 'no path set'}): Rule/Action lookups will fail. "
                            'Set "paths.alarm_map" in config.json.')
            amap = rc.AlarmIdMap()  # what recipe_checker.main() does
        data, errors = rc.check_recipe_string(filename, xml_text, amap, skip_workflow_check)
        err_text = errors.get_accumulated_error_string()
        per_rule, total = _split_blocks(errors)
    except Exception as e:  # noqa: BLE001 - the checker is user code; never take the UI down with it
        return CheckResult(text=f"Checker failed: {type(e).__name__}: {e}", ok=False, n_errors=1,
                           warnings=warnings, seconds=time.perf_counter() - t0)

    parts = [f"[warn] {w}" for w in warnings]
    if parts:
        parts.append("")
    if err_text:
        parts.append(err_text)
    else:
        parts.append(f"No errors found in recipe: {filename}")
        if data is not None:
            parts.append(rc.next_valid_overflow_token(data, amap).rstrip())
    return CheckResult(text="\n".join(parts), ok=not err_text, n_errors=total, per_rule=per_rule,
                       warnings=warnings, seconds=time.perf_counter() - t0)
