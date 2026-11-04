"""The alarms of an XOFYBIN rule as a flow: one step per <Alarm>, ``(Action, ConfigKey)`` (no UI code in here).

A rule runs its alarms in order (Order 1, 2, 3 ...), each one an Action named in AlarmIdMapping.xml.  The Action
``ConfigKey`` runs the workflows of a ConfigGroup of the pcs_die_file's List Workflow table, the one the alarm's
ConfigKey attribute names:

    HST  Socket -> ConfigKey (Check_DUT_Orientation) -> Escalate                 (tests/EMR_MCC_Die.xml)
    SST  ConfigKey (SST_ReplaceMB) -> ConfigKey (SST_ReplaceMB) -> ConfigKey (SST_Escalate)   (tests/SST_Die_ARL_HX.xml)

so the keys belong to the ConfigKey actions, in the same order, and the same action or key may come up more than once
in a rule.  What the two pickers of the rule grid (the Action and the Alarm Config Key cell) send is turned into the new
flow of the rule here: with the product HST the actions are picked, and the keys for the ConfigKey actions among them;
with SST every alarm is a ConfigKey alarm, so only the keys are picked.  ``PcsDieFileDoc.set_flow`` writes the result.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

CONFIG_KEY = "ConfigKey"
SST, HST = "SST", "HST"
#: (Action, ConfigKey) of one alarm; an empty ConfigKey = the alarm has none
Step = tuple[str, str]
#: the columns of the rule grid the pickers edit
ACTION_FIELD, KEY_FIELD = "action", "alarm_config_key"


@dataclass(frozen=True)
class Pick:
    """What a pick comes to: the rule's alarms after it, or - ``steps`` is None - why it is refused."""

    steps: list[Step] | None = None
    error: str = ""
    #: SST: the actions of the rule that the pick turns into ConfigKey (not empty = ask before writing it)
    replaced: tuple[str, ...] = ()


def actions_of(alarm_map_path: str | Path) -> list[str]:
    """The Action names of AlarmIdMapping.xml in file order - what the Action picker offers.  [] when there is no such
    file or it cannot be read (the checker says so in its own way)."""
    if not alarm_map_path:
        return []
    try:
        root = ET.parse(str(alarm_map_path)).getroot()
    except (OSError, ET.ParseError):
        return []
    names = (str(a.get("name") or "").strip() for a in root.findall("Actions/Action"))
    return list(dict.fromkeys(name for name in names if name))


def apply_pick(
    steps: list[Step],
    field: str,
    items: Iterable,
    product: str,
    *,
    actions: Iterable[str] | None = None,
    keys: Iterable[str] | None = None,
) -> Pick:
    """The flow of a rule after a pick of its Action cell (``field`` "action") or of its Alarm Config Key cell
    ("alarm_config_key").  ``steps`` is the rule's flow now, ``product`` SST or HST.

    ``items`` of the Action cell: ``[action, n]`` pairs in the order picked, ``n`` = the place (0 ...) the alarm had in
    ``steps`` - or None for an action that was just added.  The pair moves with its key, so dragging a ConfigKey action
    to another place takes its key along; an action that is no longer there takes its key with it.  An alarm that has
    no Action (a Message alarm) goes on without one; a new action always has a name.
    ``items`` of the Alarm Config Key cell: the picked keys in order.  HST: exactly one for each ConfigKey action of
    the rule, in that order - '' for an action that has none yet.  SST: one alarm per key.

    ``actions`` / ``keys`` (None = do not check): the Action names of the alarm map and the Keys of the List Workflow
    table.  A name that the rule already has is always allowed, so a pcs_die_file with an odd value can be edited."""
    kind = (product or "").strip().upper()
    if kind not in (SST, HST):
        return Pick(error=f"Unknown product {product!r}: it must be SST or HST.")
    if field == ACTION_FIELD:
        return _pick_actions(steps, items, kind, None if actions is None else set(actions))
    if field == KEY_FIELD:
        return _pick_keys(steps, items, kind, None if keys is None else set(keys))
    return Pick(error=f"The pickers do not edit the {field} column.")


def _pick_actions(steps: list[Step], items: Iterable, kind: str, allowed: set[str] | None) -> Pick:
    if kind == SST:
        return Pick(error="In SST the action is always ConfigKey - pick the Alarm Config Key instead.")
    picks: list[tuple[str, int | None]] = []
    for item in items:
        try:
            action, origin = (item, None) if isinstance(item, str) else item
            action = str(action).strip()
            ok = origin is None or (type(origin) is int and origin >= 0)
        except (TypeError, ValueError):
            ok = False
        if ok and not action:  # only an alarm without an Action (a Message alarm) may go on without one
            ok = origin is not None and origin < len(steps) and steps[origin][0] == ""
        if not ok:
            return Pick(error=f"Not an action pick: {item!r}.")
        picks.append((action, origin))
    if not picks:
        return Pick(error="A rule needs at least one action.")
    if allowed is not None:
        known = allowed | {action for action, _ in steps}
        for action, _ in picks:
            if action not in known:
                return Pick(error=f"Action {action} is not in the alarm map (AlarmIdMapping.xml).")
    new: list[Step] = []
    for action, origin in picks:
        same = origin is not None and origin < len(steps) and steps[origin][0] == action
        new.append((action, steps[origin][1] if same else ""))
    return Pick(new)


def _pick_keys(steps: list[Step], items: Iterable, kind: str, allowed: set[str] | None) -> Pick:
    picked: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return Pick(error=f"Not a key pick: {item!r}.")
        picked.append(item.strip())
    if allowed is not None:
        known = allowed | {key for _, key in steps}
        for key in picked:
            if key and key not in known:
                return Pick(error=f"Key {key} is not in the List Workflow table.")
    if kind == SST:
        picked = [key for key in picked if key]
        if not picked:
            return Pick(error="Pick at least one ConfigKey.")
        replaced = tuple(dict.fromkeys(action for action, _ in steps if action != CONFIG_KEY))
        return Pick([(CONFIG_KEY, key) for key in picked], replaced=replaced)
    slots = [i for i, (action, _) in enumerate(steps) if action == CONFIG_KEY]
    if not slots:
        return Pick(error="This rule has no ConfigKey action - add one to its Action first.")
    if len(picked) != len(slots):
        return Pick(error=f"This rule has {len(slots)} ConfigKey action(s): a pick is {len(slots)} key(s), '' for one without.")
    new = list(steps)
    for key, i in zip(picked, slots):  # the alarms that are not ConfigKey alarms keep whatever they have
        new[i] = (CONFIG_KEY, key)
    return Pick(new)
