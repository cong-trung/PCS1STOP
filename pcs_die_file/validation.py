"""Thin adapter around ``pcs_die_file_checker`` (the Python port of the iLLIAD checker).

``check_pcs_die_file_string`` only reports problems as formatted text blocks.  This module runs it on the *edited*
pcs_die_file text and additionally maps every block that belongs to an XOFYBIN rule to that rule's ``RuleName`` so
the grid can show a per-row "Validation Error".  Nothing in pcs_die_file_checker.py is modified or duplicated.

After the checker comes one more step, the duplicate bins (``pcs_die_file.duplicates``): an XOFYBIN rule whose bins lie
completely inside another rule's bins is reported as a *warning* - a note on its row and the text of the Warning tab.
Warnings never make the check fail (``CheckResult.ok``), so the pcs_die_file can still be saved.

The last step is the bins of the queried test program (``pcs_die_file.missing_bins``): the ones no rule has are listed in
the Missing Bins tab.  The same check runs a second time on the bins the program's units had (the unit level data), for
the tab next to it.  That is information too - it never makes the check fail either.
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pcs_die_file import duplicates as dup
from pcs_die_file import missing_bins as mb
from pcs_die_file import pcs_die_file_checker as rc

#: parent rules a Note names before it says "+N more" (the Warning tab lists them all)
NOTE_PARENTS = 3


@dataclass
class CheckResult:
    text: str                     # what the Error tab shows
    ok: bool                      # no errors at all (warnings and missing bins do not count)
    n_errors: int = 0
    per_rule: dict[str, list[str]] = field(default_factory=dict)   # XOFYBIN RuleName -> error lines
    warnings: list[str] = field(default_factory=list)   # problems with the setup (no alarm map, ...): shown as [warn] in the Error tab
    seconds: float = 0.0
    duplicates: list[dup.Duplicate] = field(default_factory=list)  # rules that lie inside another rule: the warnings
    notes: dict[str, str] = field(default_factory=dict)            # row id of such a rule -> its Note cell
    warning_text: str = ""                                         # what the Warning tab shows
    missing: list[int] = field(default_factory=list)  # bin numbers of the program that no rule has (an FB before its DBs)
    n_program_bins: int = 0                           # distinct bin numbers the program has (0: nothing was checked)
    missing_text: str = ""                            # what the Missing Bins tab shows
    unit_missing: list[int] = field(default_factory=list)  # the same for the bins the program's units had
    n_unit_bins: int = 0                                    # distinct bin numbers its units had (0: nothing was checked)
    unit_missing_text: str = ""                             # what the unit level tab shows

    @property
    def n_warnings(self) -> int:
        """How many rules lie inside another rule (what the Warning tab lists)."""
        return len(self.duplicates)

    @property
    def n_missing(self) -> int:
        """How many bin numbers of the program no rule has (what the Missing Bins tab lists)."""
        return len(self.missing)

    @property
    def n_unit_missing(self) -> int:
        """How many bin numbers of its units no rule has (what the unit level tab lists)."""
        return len(self.unit_missing)


_NODE_TAG = re.compile(r"<\s*([A-Za-z_][\w.\-]*)")
_RULE_NAME = re.compile(r'RuleName="([^"]*)"')


def _split_blocks(errors) -> tuple[dict[str, list[str]], int]:
    """Parse PcsDieFileErrorTracker.accumulated: ' PCS die file Node: ...\\n<xml>\\n Errors:\\n     msg\\n     msg'."""
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


def _label(rule: dup.BinRule) -> str:
    return rule.name or "(no RuleName)"


def _note(found: dup.Duplicate) -> str:
    """The Note cell of a duplicate: which rule(s) it lies inside of."""
    names = [_label(parent) for parent in found.parents]
    more = f" +{len(names) - NOTE_PARENTS} more" if len(names) > NOTE_PARENTS else ""
    return "Duplicate of " + ", ".join(names[:NOTE_PARENTS]) + more


def _warning_text(found: list[dup.Duplicate], filename: str) -> str:
    """The Warning tab: every duplicate with its bins and ranges and the rule(s) it lies inside of."""
    if not found:
        return f"No warnings found in pcs_die_file: {filename}"
    lines = [
        f"{len(found)} rule(s) lie completely inside the bins of another rule.",
        "Only a warning: the pcs_die_file can still be saved.",
        "(A [bin] in Bins is a carve-out - a rule that sits in one is no duplicate.  LowYield rules are skipped.)",
        "",
    ]
    for item in found:
        lines += [
            _label(item.rule),
            f"    Bins:          {item.rule.bins}",
            f"    Ranges:        {dup.format_ranges(item.rule.ranges)}",
        ]
        for parent in item.parents:
            lines += [
                f"    Duplicate of:  {_label(parent)}",
                f"        Bins:      {parent.bins}",
                f"        Ranges:    {dup.format_ranges(parent.ranges)}",
            ]
        lines.append("")
    return "\n".join(lines).rstrip()


def _find_duplicates(bin_rules: list[tuple[str, str, str]] | None, warnings: list[str]) -> list[dup.Duplicate]:
    if bin_rules is None:
        return []
    try:
        return dup.find_duplicates(bin_rules)
    except Exception as e:  # noqa: BLE001 - a warning step must never take the whole check down
        warnings.append(f"Duplicate bins were not checked: {type(e).__name__}: {e}")
        return []


def _missing_text(found: mb.ProgramCoverage | None, error: str, *, units: bool = False) -> str:
    """The Missing Bins tab: how many bins of the TP no rule has, then those bin numbers, one per line.  ``units``: the tab
    of the bins its units had (the unit level data) instead of the bins the TP defines."""
    if error:
        return f"The {'unit level bins' if units else 'bins'} of the TP were not checked: {error}"
    if found is None:
        if units:
            return "Type a TP name in the TP Name box: the bins its units had that no rule has are listed here."
        return "Type a TP name in the TP Name box: the bins of that TP that no rule has are listed here."
    who = "TP " + (", ".join(f"'{name}'" for name in found.programs) or "(no name)") + (" (unit level data)" if units else "")
    if not found.checked:
        head = (
            f"{who}: the CSV has no bin number to check."
            if found.programs
            else "The CSV has no bin number to check."  # (header only: the TP has no bins)
        )
    elif found.missing:
        head = f"{who}: {len(found.missing)} of {found.checked} bin(s) are in no rule."
    else:
        head = f"{who}: all {found.checked} bin(s) are in a rule."
    lines = [head]
    if units:
        lines.append("Every unit counts with its most detailed bin: the data bin, else the functional bin, else the interface bin.")
    lines += [
        "A bin is in a rule when the Bins of the rules - without their [ ] parts - cover all of it: 9103 is 91030000-91039999.",
        "LowYield rules do not count.  Sorted by bin: an FB comes before its DBs.",
    ]
    if found.ignored:
        lines.append(f"{found.ignored} row(s) of the CSV are no bin number (1 to 8 digits) and were left out.")
    return "\n".join([*lines, "", *map(str, found.missing)]).rstrip()


def _check_missing(bin_rules: list[tuple[str, str, str]] | None, program_csv: str | Path | None,
                   warnings: list[str], step: str = "Missing bins") -> tuple[mb.ProgramCoverage | None, str]:
    """(the bins of the CSV against the rules, why that could not be told - '' when it could)."""
    if bin_rules is None or program_csv is None:
        return None, ""
    try:
        return mb.find_missing(bin_rules, program_csv), ""
    except mb.MissingBinsError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - an information step must never take the whole check down
        message = f"{type(e).__name__}: {e}"
        warnings.append(f"{step} were not checked: {message}")
        return None, message


def run_check(xml_text: str, filename: str, alarm_map_path: str = "", skip_workflow_check: bool = True,
              bin_digit_exceptions: dict | None = None,
              bin_rules: Iterable[tuple[str, str, str]] | None = None,
              program_csv: str | Path | None = None,
              unit_csv: str | Path | None = None) -> CheckResult:
    """Run pcs_die_file_checker on ``xml_text``.  The paths/options come from the caller (config.json), none live here.

    ``bin_rules``: (row id, RuleName, Bins) of every XOFYBIN rule (``PcsDieFileDoc.bin_rules()``) - the duplicate-bins
    step runs on them after the checker; None = that step is left out.
    ``program_csv``: the CSV the TP Name box saved (PROGRAM_NAME, BIN_NUMBER) - its bins that none of those rules has go
    to the Missing Bins tab; None = that step is left out.
    ``unit_csv``: the same for the CSV of the bins the TP's units had - the unit level tab."""
    t0 = time.perf_counter()
    warnings: list[str] = []
    rules = None if bin_rules is None else list(bin_rules)
    found = _find_duplicates(rules, warnings)
    coverage, why = _check_missing(rules, program_csv, warnings)
    unit_coverage, unit_why = _check_missing(rules, unit_csv, warnings, "Unit level missing bins")
    extra = {"duplicates": found, "notes": {item.rule.key: _note(item) for item in found},
             "warning_text": _warning_text(found, filename),
             "missing": list(coverage.missing) if coverage else [],
             "n_program_bins": coverage.checked if coverage else 0,
             "missing_text": _missing_text(coverage, why),
             "unit_missing": list(unit_coverage.missing) if unit_coverage else [],
             "n_unit_bins": unit_coverage.checked if unit_coverage else 0,
             "unit_missing_text": _missing_text(unit_coverage, unit_why, units=True)}
    try:
        amap = rc.AlarmIdMap.load(alarm_map_path) if alarm_map_path else None
        if amap is None:
            warnings.append(f"Alarm map not found ({alarm_map_path or 'no path set'}): Rule/Action lookups will fail. "
                            'Set "paths.alarm_map" in config.json.')
            amap = rc.AlarmIdMap()  # what pcs_die_file_checker.main() does
        data, errors = rc.check_pcs_die_file_string(filename, xml_text, amap, skip_workflow_check, bin_digit_exceptions)
        err_text = errors.get_accumulated_error_string()
        per_rule, total = _split_blocks(errors)
    except Exception as e:  # noqa: BLE001 - the checker is user code; never take the UI down with it
        return CheckResult(text=f"Checker failed: {type(e).__name__}: {e}", ok=False, n_errors=1,
                           warnings=warnings, seconds=time.perf_counter() - t0, **extra)

    parts = [f"[warn] {w}" for w in warnings]
    if parts:
        parts.append("")
    if err_text:
        parts.append(err_text)
    else:
        parts.append(f"No errors found in pcs_die_file: {filename}")
        if data is not None:
            parts.append(rc.next_valid_overflow_token(data, amap).rstrip())
    return CheckResult(text="\n".join(parts), ok=not err_text, n_errors=total, per_rule=per_rule,
                       warnings=warnings, seconds=time.perf_counter() - t0, **extra)
