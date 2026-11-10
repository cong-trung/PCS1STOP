"""Duplicate bin ranges between XOFYBIN rules - the logic of dupilcate.md, as a step of the validation.

A rule's ``Bins`` says which bins it watches: ``IB70,[FB7056]`` is every bin of IB70 except FB7056 - what stands in
``[ ]`` is a deliberate carve-out.  To compare rules, the bins are turned into start / end ranges in the 8 digits of a DB bin,
the carve-outs are cut out, and what is left - the rule's *effective ranges* - is compared, never the text:

    DB88050218    88050218-88050218
    FB7006        70060000-70069999      (padded with 0 for the start, with 9 for the end)
    FB8090-8099   80900000-80999999
    IB70          70000000-70999999      IB9-99  90000000-99999999

A rule whose effective ranges lie completely inside those of another rule is a FULL_DUPLICATE of it; two rules with the
same ranges are duplicates of each other.  That is all this module reports.  A rule that only overlaps another one
(PARTIAL_OVERLAP) is left alone, and a rule that sits in a hole cut out of another (EXPLICIT_EXCEPTION: ``IB70,[FB7056]``
and ``FB7056``) cannot be a duplicate of it, because the hole is not part of the other's effective ranges.  Rules with
``LowYield`` in their name (any case) are not looked at, and neither is a rule whose Bins cannot be read - the checker
reports those.  Duplicates are only looked for inside the same ``ValidOperations`` of the rules' ``<RuleGroup>``: two
rules of different values (``""`` - no such attribute - is a value like any other) are never compared, so the same bins
under two different operations are no duplicates.
"""
from __future__ import annotations

import re
from bisect import bisect_left, insort
from collections.abc import Iterable
from dataclasses import dataclass

#: digits of a DB bin; an IB (2 digits) or FB (4 digits) bin is the start of such a number
DIGITS = 8
#: rules with this in their name (any case) are skipped
SKIPPED_NAME = "lowyield"

_BIN = re.compile(r"(?:IB|FB|DB)(\d+)(?:-(\d+))?", re.IGNORECASE)


@dataclass(frozen=True)
class Range:
    start: int
    end: int

    def __str__(self) -> str:
        return f"{self.start:0{DIGITS}d}-{self.end:0{DIGITS}d}"


@dataclass(frozen=True)
class BinRule:
    key: str  # what the caller calls the rule (the id of its row)
    name: str  # RuleName
    bins: str  # the Bins expression, as written
    valid_operations: str  # the ValidOperations of its <RuleGroup> ("" when there is none): only rules of the same value meet
    ranges: tuple[Range, ...]  # effective ranges: sorted, apart from each other, carve-outs cut out


@dataclass(frozen=True)
class Duplicate:
    rule: BinRule
    parents: tuple[BinRule, ...]  # the rules its effective ranges lie inside of, in file order


def format_ranges(ranges: Iterable[Range]) -> str:
    return ";".join(map(str, ranges))


def bin_range(token: str) -> Range | None:
    """``FB7006`` -> 70060000-70069999, ``IB9-99`` -> 90000000-99999999, ``DB88050218`` -> 88050218-88050218.
    None for what is no bin, has more than 8 digits or runs backwards."""
    found = _BIN.fullmatch(token.strip())
    if found is None:
        return None
    low, high = found.group(1), found.group(2) or found.group(1)
    if max(len(low), len(high)) > DIGITS:
        return None
    start, end = int(low.ljust(DIGITS, "0")), int(high.ljust(DIGITS, "9"))
    return Range(start, end) if start <= end else None


def split_bins(expression: str, *, strict: bool = True) -> tuple[list[Range], list[Range]] | None:
    """(ranges of the bins, ranges of the ``[carve-outs]``) of a Bins value.  A token that is no usable bin makes the
    whole value unusable (None) - or is skipped with ``strict=False``."""
    normal: list[Range] = []
    carved: list[Range] = []
    for raw in expression.split(","):
        token = raw.strip()
        carve = token.startswith("[") and token.endswith("]")
        found = bin_range(token[1:-1] if carve else token)
        if found is None:
            if strict:
                return None
            continue
        (carved if carve else normal).append(found)
    return normal, carved


def merge(ranges: Iterable[Range]) -> list[Range]:
    """Sorted, with overlapping and touching ranges joined (70000000-70009999 + 70010000-70019999 -> 70000000-70019999)."""
    out: list[Range] = []
    for r in sorted(ranges, key=lambda r: (r.start, r.end)):
        if out and r.start <= out[-1].end + 1:
            if r.end > out[-1].end:
                out[-1] = Range(out[-1].start, r.end)
        else:
            out.append(r)
    return out


def effective_ranges(normal: Iterable[Range], carved: Iterable[Range]) -> list[Range]:
    """The bins of a rule without its carve-outs: 70000000-70999999 minus 70560000-70569999 is 70000000-70559999 and
    70570000-70999999."""
    ranges = merge(normal)
    for hole in carved:
        cut: list[Range] = []
        for r in ranges:
            if hole.end < r.start or hole.start > r.end:
                cut.append(r)
                continue
            if r.start < hole.start:
                cut.append(Range(r.start, hole.start - 1))
            if hole.end < r.end:
                cut.append(Range(hole.end + 1, r.end))
        ranges = cut
    return merge(ranges)


def find_duplicates(rules: Iterable[tuple[str, str, str, str]]) -> list[Duplicate]:
    """``rules``: (key, RuleName, Bins, ValidOperations) of every XOFYBIN rule, in file order.  Returns the rules whose
    effective ranges lie completely inside those of another rule - with that rule - in file order.  Only rules of the
    same Valid Operations are compared with each other."""
    items: list[BinRule] = []
    for key, name, bins, valid_operations in rules:
        name, bins = (name or "").strip(), (bins or "").strip()
        ops = (valid_operations or "").strip()
        parsed = None if SKIPPED_NAME in name.lower() else split_bins(bins)
        ranges = effective_ranges(*parsed) if parsed else []
        if ranges:
            items.append(BinRule(key, name, bins, ops, tuple(ranges)))

    groups: dict[str, list[int]] = {}
    for i, item in enumerate(items):
        groups.setdefault(item.valid_operations, []).append(i)

    result: dict[int, Duplicate] = {}
    for indices in groups.values():  # one Valid Operations value at a time: the groups never meet
        holders = _holders(items, indices)
        for i in indices:
            if holders[i]:
                result[i] = Duplicate(items[i], tuple(items[p] for p in sorted(holders[i])))
    return [result[i] for i in sorted(result)]


def _holders(items: list[BinRule], indices: list[int]) -> dict[int, set[int]]:
    """Which of the rules in ``indices`` (one Valid Operations group) each of them lies inside of.

    A rule is inside another when each of its ranges is: sweep the ranges by start, keep the ends of everything that
    started before (sorted), and what ends at or after the range asked about holds it.  Only the holders are looked at."""
    spans = sorted((r.start, r.end, i) for i in indices for r in items[i].ranges)
    reach: list[tuple[int, int]] = []  # (end, rule) of every range that starts at or before the one asked about
    holders: dict[int, set[int]] = {}
    at = 0
    while at < len(spans):
        stop = at
        while stop < len(spans) and spans[stop][0] == spans[at][0]:  # ranges that start together hold each other
            insort(reach, (spans[stop][1], spans[stop][2]))
            stop += 1
        for _, end, i in spans[at:stop]:
            around = {rule for _, rule in reach[bisect_left(reach, (end, -1)) :] if rule != i}
            holders[i] = around if i not in holders else holders[i] & around
        at = stop
    return holders
