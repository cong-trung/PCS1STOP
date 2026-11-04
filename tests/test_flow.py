"""The flow of an XOFYBIN rule: what the Action / Alarm Config Key pickers send (``flow.apply_pick``) and how the model
writes it (``PcsDieFileDoc.set_flow``).  The real pcs_die_files of the tests folder are used where they are there."""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from pcs_die_file import flow as fl
from pcs_die_file.model import PcsDieFileDoc

CK = fl.CONFIG_KEY
HERE = Path(__file__).parent
ALARM_MAP = HERE.parent / "DpcConfig" / "V1.00" / "AlarmIdMapping.xml"
EMR = HERE / "EMR_MCC_Die.xml"  # HST: Socket -> ConfigKey -> Escalate
SST = HERE / "SST_Die_ARL_HX.xml"  # SST: every alarm is a ConfigKey alarm
needs_emr = pytest.mark.skipif(not EMR.is_file(), reason="tests/EMR_MCC_Die.xml is not part of this checkout")
needs_sst = pytest.mark.skipif(not SST.is_file(), reason="tests/SST_Die_ARL_HX.xml is not part of this checkout")
LAYOUTS = [("\n", "  "), ("\r\n", "\t"), ("", "")]  # LF + spaces, CRLF + tabs, everything on one line


# --------------------------------------------------------------------------------------------- apply_pick
HST_FLOW = [("Socket", ""), (CK, "Check_DUT_Orientation"), ("Escalate", "")]  # the CheckDUTOrientation rule of EMR_MCC_Die.xml


def pick(steps, field, items, product="HST", **known):
    return fl.apply_pick(steps, field, items, product, **known)


def test_a_configkey_action_dragged_to_another_place_takes_its_key_along():
    got = pick(HST_FLOW, "action", [[CK, 1], ["Socket", 0], ["Escalate", 2]])
    assert got.steps == [(CK, "Check_DUT_Orientation"), ("Socket", ""), ("Escalate", "")] and got.error == ""


def test_an_action_that_was_added_has_no_key_and_one_that_was_taken_out_takes_its_key_with_it():
    added = pick(HST_FLOW, "action", [["Socket", 0], [CK, None], [CK, 1], ["Escalate", 2]])
    assert added.steps == [("Socket", ""), (CK, ""), (CK, "Check_DUT_Orientation"), ("Escalate", "")]
    gone = pick(HST_FLOW, "action", [["Socket", 0], ["Escalate", 2]])
    assert gone.steps == [("Socket", ""), ("Escalate", "")]


def test_the_keys_stay_in_the_slots_of_their_actions_whatever_the_other_slots_hold():
    flow = [(CK, ""), (CK, "K2"), ("Escalate", "")]  # the first ConfigKey action has no key yet
    got = pick(flow, "action", [["Socket", None], [CK, 0], [CK, 1], ["Escalate", 2]])
    assert got.steps == [("Socket", ""), (CK, ""), (CK, "K2"), ("Escalate", "")]
    swapped = pick(flow, "action", [[CK, 1], [CK, 0], ["Escalate", 2]])
    assert swapped.steps == [(CK, "K2"), (CK, ""), ("Escalate", "")]  # the pair moves, whatever it holds


def test_a_key_on_an_action_that_is_not_configkey_is_not_thrown_away():
    odd = [("Abc", "SST_ReplaceMB"), (CK, "SST_Escalate")]  # (the SST sample has such a rule)
    got = pick(odd, "action", [[CK, 1], ["Abc", 0]])
    assert got.steps == [(CK, "SST_Escalate"), ("Abc", "SST_ReplaceMB")]


def test_a_pick_that_names_another_action_than_the_alarm_it_came_from_gets_no_key():
    assert pick(HST_FLOW, "action", [["Escalate", 1]]).steps == [("Escalate", "")]
    assert pick(HST_FLOW, "action", [["Escalate", 99]]).steps == [("Escalate", "")]


def test_the_picked_actions_are_checked_against_the_alarm_map_but_the_ones_the_rule_has_are_fine():
    known = {"Socket", "Escalate", CK}
    flow = [("Abc", ""), ("Socket", "")]
    assert pick(flow, "action", [["Abc", 0], ["Escalate", None]], actions=known).steps == [("Abc", ""), ("Escalate", "")]
    refused = pick(flow, "action", [["Reboot", None]], actions=known)
    assert refused.steps is None and "Reboot" in refused.error and "AlarmIdMapping.xml" in refused.error
    assert pick(flow, "action", [["Reboot", None]]).steps == [("Reboot", "")]  # None = not checked


@pytest.mark.parametrize("items", [[], [["", 0]], [["Socket", "x"]], [["Socket", -1]], [["Socket", True]], [123], [["a", 1, 2]], [["", None]]])
def test_an_action_pick_that_makes_no_sense_is_refused(items):
    got = pick(HST_FLOW, "action", items)
    assert got.steps is None and got.error


def test_an_alarm_without_an_action_goes_on_without_one_but_a_new_action_has_a_name():
    flow = [("", "K"), ("Socket", ""), ("Escalate", "")]  # a Message alarm: no Action
    got = pick(flow, "action", [["", 0], ["Socket", 1], ["Reboot", None], ["Escalate", 2]], actions={"Socket", "Reboot", "Escalate"})
    assert got.steps == [("", "K"), ("Socket", ""), ("Reboot", ""), ("Escalate", "")]
    assert pick(flow, "action", [["Socket", 1], ["", 0]]).steps == [("Socket", ""), ("", "K")]  # (and may be moved)
    assert pick(flow, "action", [["", None]]).steps is None  # (a new one may not)
    assert pick(flow, "action", [["", 1]]).steps is None  # (nor may another alarm's place be taken for it)


def test_an_action_pick_may_name_the_actions_as_plain_strings():
    assert pick(HST_FLOW, "action", ["Socket", "Escalate"]).steps == [("Socket", ""), ("Escalate", "")]


def test_in_sst_the_action_is_always_configkey_so_it_is_not_picked():
    got = pick(HST_FLOW, "action", [["Socket", 0]], "SST")
    assert got.steps is None and "ConfigKey" in got.error


def test_hst_keys_go_to_the_configkey_actions_in_order_and_the_other_alarms_keep_what_they_have():
    flow = [("Socket", ""), (CK, ""), ("Escalate", "stray"), (CK, "old")]
    got = pick(flow, "alarm_config_key", ["A", "B"])
    assert got.steps == [("Socket", ""), (CK, "A"), ("Escalate", "stray"), (CK, "B")]
    assert pick(flow, "alarm_config_key", ["", "B"]).steps == [("Socket", ""), (CK, ""), ("Escalate", "stray"), (CK, "B")]
    assert pick(flow, "alarm_config_key", ["", ""]).steps == [("Socket", ""), (CK, ""), ("Escalate", "stray"), (CK, "")]


def test_hst_keys_are_one_for_each_configkey_action_not_a_key_more_or_less():
    flow = [("Socket", ""), (CK, "old"), ("Escalate", ""), (CK, "")]
    for items in ([], ["A"], ["A", "B", "C"]):  # (a list for another number of slots is a stale picker: nothing is guessed)
        got = pick(flow, "alarm_config_key", items)
        assert got.steps is None and "2 ConfigKey action(s)" in got.error, items
    none = pick([("Socket", ""), ("Escalate", "")], "alarm_config_key", ["A"])
    assert none.steps is None and "no ConfigKey action" in none.error


def test_a_picked_key_must_be_in_the_list_workflow_table_unless_the_rule_has_it_already():
    keys = {"K1", "K2"}
    assert pick(HST_FLOW, "alarm_config_key", ["K2"], keys=keys).steps[1] == (CK, "K2")
    assert pick(HST_FLOW, "alarm_config_key", ["Check_DUT_Orientation"], keys=keys).steps[1][1] == "Check_DUT_Orientation"
    refused = pick(HST_FLOW, "alarm_config_key", ["Nope"], keys=keys)
    assert refused.steps is None and "Nope" in refused.error and "List Workflow" in refused.error
    assert pick(HST_FLOW, "alarm_config_key", ["Nope"]).steps[1] == (CK, "Nope")  # None = not checked


def test_sst_has_one_configkey_alarm_for_every_key_and_a_key_may_come_up_again():
    flow = [(CK, "SST_ReplaceMB"), (CK, "SST_ReplaceMB"), (CK, "SST_Escalate")]
    assert pick(flow, "alarm_config_key", ["SST_ReplaceMB", "SST_Escalate"], "SST").steps == [(CK, "SST_ReplaceMB"), (CK, "SST_Escalate")]
    got = pick(flow, "alarm_config_key", ["A", "A", "B", "B"], "SST")
    assert got.steps == [(CK, "A"), (CK, "A"), (CK, "B"), (CK, "B")] and got.replaced == ()
    assert pick([(CK, "x")], "alarm_config_key", [" A "], "SST").steps == [(CK, "A")]


def test_sst_says_which_actions_it_would_turn_into_configkey():
    flow = [("Socket", ""), ("Escalate", ""), ("Socket", ""), (CK, "K")]
    got = pick(flow, "alarm_config_key", ["K"], "SST")
    assert got.steps == [(CK, "K")] and got.replaced == ("Socket", "Escalate")  # (once each, in order)


def test_sst_needs_at_least_one_key():
    for items in ([], [""]):
        got = pick([(CK, "K")], "alarm_config_key", items, "SST")
        assert got.steps is None and "at least one" in got.error


def test_the_product_may_be_written_in_any_case_and_nothing_else_is_a_product():
    assert pick(HST_FLOW, "action", [["Socket", 0]], " hst ").steps == [("Socket", "")]
    assert pick(HST_FLOW, "action", [["Socket", 0]], "XST").steps is None
    assert pick(HST_FLOW, "comment", ["x"]).steps is None


def test_a_key_that_is_no_text_is_refused():
    assert pick(HST_FLOW, "alarm_config_key", [None]).steps is None


# ----------------------------------------------------------------------------------------------- actions_of
def test_the_actions_come_from_the_alarm_map_in_file_order():
    names = fl.actions_of(ALARM_MAP)
    assert names[:3] == ["AP", "TIU", "Tester"] and CK in names and "Socket" in names and "Escalate" in names
    assert len(names) == len(set(names)) == 41


def test_no_alarm_map_no_actions(tmp_path):
    assert fl.actions_of("") == [] and fl.actions_of(tmp_path / "nowhere.xml") == []
    broken = tmp_path / "broken.xml"
    broken.write_text("<AlarmIdMapping><Actions>", encoding="utf-8")
    assert fl.actions_of(broken) == []


def test_the_actions_do_not_depend_on_the_rest_of_the_alarm_map(tmp_path):
    odd = tmp_path / "odd.xml"  # (the checker cannot read this BinRange: the Action picker still has its actions)
    odd.write_text(
        '<AlarmIdMapping><BinRange>1-99, abc</BinRange><Actions><Action name="AP" id="001"/>'
        '<Action name=" Socket " id="002"/><Action name="AP" id="003"/><Action id="004"/></Actions></AlarmIdMapping>',
        encoding="utf-8",
    )
    assert fl.actions_of(odd) == ["AP", "Socket"]  # (once each, trimmed, a nameless one left out)


# ----------------------------------------------------------------------------------------------------- the model
def flow_xml(*rules, groups=("K1", "K2", "A;B"), eol: str = "\n", unit: str = "  ") -> str:
    """XOFYBIN rules as the real files lay them out: (RuleName, steps) - one <Alarm> per step, (Action, ConfigKey) - in a
    <RepeatTriggerGroup>; ``groups`` are the Keys of the List Workflow table."""

    def alarm(i: int, step: tuple[str, str]) -> str:
        action, key = step
        extra = f' ConfigKey="{key}"' if key else ""
        return f'<Alarm Order="{i}" Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="B" Action="{action}"{extra} />'

    lines = [
        '<ComponentRecipe checksum="x">',
        f'{unit}<Identity type="iLLIAD" datagroup="SST_X" name="Die" id="1" version="1.0" asof="2024-01-01T00:00:00" />',
        f"{unit}<SharedConfigurations>",
    ]
    lines += [f'{unit * 2}<ConfigGroup Key="{k}"><Workflow Path="M:\\wf\\{k}.xml" /></ConfigGroup>' for k in groups]
    lines += [f"{unit}</SharedConfigurations>", f'{unit}<RuleGroup GroupName="XofYBinGroup" ValidOperations="">']
    for name, steps in rules:
        lines.append(f'{unit * 2}<XOFYBIN Bins="FB7001" X="4" Y="5" RuleName="{name}">')
        lines.append(f'{unit * 3}<RepeatTriggerGroup RepeatTriggerHistoryLength="3">')
        lines += [f"{unit * 4}{alarm(i, step)}" for i, step in enumerate(steps, 1)]
        lines += [f"{unit * 3}</RepeatTriggerGroup>", f"{unit * 2}</XOFYBIN>"]
    lines += [f"{unit}</RuleGroup>", "</ComponentRecipe>"]
    return eol.join(lines) + eol


def flow_doc(*rules, **layout) -> PcsDieFileDoc:
    return PcsDieFileDoc(flow_xml(*rules, **layout).encode("utf-8"), "r.xml")


def cells(updates) -> set[tuple[str, str, str]]:
    return {(u.row_id, u.field, u.value) for u in updates}


def test_the_flow_of_a_rule_is_one_step_per_alarm_where_the_cell_text_collapses_equal_values():
    doc = flow_doc(("A", [("Socket", ""), ("Socket", "")]), ("B", HST_FLOW))
    assert doc.flow("xofy", "x0") == [("Socket", ""), ("Socket", "")]
    assert doc.rows("xofy")[0]["action"] == "Socket"  # (the cell reads the same for one alarm and for three)
    assert doc.rows("xofy")[0]["_flow"] == [["Socket", ""], ["Socket", ""]]
    assert doc.rows("xofy")[1]["_flow"] == [list(step) for step in HST_FLOW]
    assert doc.rows("xofy")[1]["alarm_config_key"] == "; Check_DUT_Orientation; "


def test_only_a_rule_with_alarms_has_a_flow():
    doc = flow_doc(("A", [("Socket", "")]))
    assert doc.flow("xofy", "x9") is None and doc.flow("workflow", "w0") is None and doc.flow("nothing", "x0") is None
    xml = flow_xml(("A", [("Socket", "")])).replace("<Alarm ", "<Other ")
    bare = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    assert bare.flow("xofy", "x0") is None and bare.rows("xofy")[0]["_flow"] == []
    assert "_flow" not in bare.rows("workflow")[0]


def test_the_workflow_keys_are_distinct_non_empty_and_in_file_order():
    doc = flow_doc(("A", [("Socket", "")]), groups=("K2", "K1", "K2", ""))
    assert doc.workflow_keys() == ["K2", "K1"]


@pytest.mark.parametrize(("eol", "unit"), LAYOUTS)
def test_a_longer_flow_gives_the_rule_more_alarms(eol, unit):
    doc = flow_doc(("A", [("Socket", ""), ("Escalate", "")]), eol=eol, unit=unit)
    res = doc.set_flow("xofy", "x0", [("Socket", ""), (CK, "K1"), (CK, "K2"), ("Escalate", "")])
    assert (res.applied, res.errors) == (1, [])
    assert doc.flow("xofy", "x0") == [("Socket", ""), (CK, "K1"), (CK, "K2"), ("Escalate", "")]
    steps = [("Socket", ""), (CK, "K1"), (CK, "K2"), ("Escalate", "")]
    assert doc.export_bytes() == flow_xml(("A", steps), eol=eol, unit=unit).encode("utf-8") and doc.export_mode == "patch"


@pytest.mark.parametrize(("eol", "unit"), LAYOUTS)
def test_a_one_step_flow_takes_a_rule_down_to_one_alarm(eol, unit):
    doc = flow_doc(("A", [("Socket", ""), (CK, "K1"), ("Escalate", "")]), eol=eol, unit=unit)
    assert doc.set_flow("xofy", "x0", [("Escalate", "")]).applied == 1  # (a text edit "Escalate" would keep all three)
    assert doc.flow("xofy", "x0") == [("Escalate", "")]
    assert doc.rows("xofy")[0]["action"] == "Escalate" and doc.rows("xofy")[0]["alarm_config_key"] == ""
    assert doc.export_bytes() == flow_xml(("A", [("Escalate", "")]), eol=eol, unit=unit).encode("utf-8")


def test_a_flow_as_long_as_the_rule_only_changes_the_values():
    doc = flow_doc(("A", [("Socket", ""), (CK, "K1"), ("Escalate", "")]))
    assert doc.set_flow("xofy", "x0", [(CK, "K2"), ("Socket", ""), ("Escalate", "")]).applied == 1
    assert doc.flow("xofy", "x0") == [(CK, "K2"), ("Socket", ""), ("Escalate", "")]
    root = etree.fromstring(doc.current_text().encode("utf-8"))
    assert [a.get("Order") for a in root.iter("Alarm")] == ["1", "2", "3"]  # the alarms stay where they are, numbered as before
    assert doc.export_mode == "" and doc.export_bytes() and doc.export_mode == "patch"


def test_the_same_flow_changes_nothing():
    doc = flow_doc(("A", [("Socket", ""), (CK, "K1")]))
    res = doc.set_flow("xofy", "x0", [("Socket", ""), (CK, "K1")])
    assert (res.applied, res.unchanged, res.updates) == (0, 1, []) and not doc.can_undo and not doc.dirty


def test_two_alarms_that_read_the_same_are_not_one_alarm_to_the_model():
    doc = flow_doc(("A", [("Socket", ""), ("Socket", "")]))
    assert doc.set_flow("xofy", "x0", [("Socket", "")]).applied == 1 and len(doc.flow("xofy", "x0")) == 1
    assert doc.set_flow("xofy", "x0", [("Socket", ""), ("Socket", "")]).applied == 1  # and back to two


def test_all_the_cells_made_of_the_alarms_are_sent_again():
    doc = flow_doc(("A", [("Socket", ""), (CK, "K1")]))
    res = doc.set_flow("xofy", "x0", [("Socket", ""), (CK, "K2"), (CK, "K1")])
    assert cells(res.updates) == {
        ("x0", "action", "Socket; ConfigKey; ConfigKey"),
        ("x0", "alarm_config_key", "; K2; K1"),
        ("x0", "bin_or_token", "B"),
    }  # (what is not made of the alarms - Bins, X, RuleName ... - is not)


def test_a_flow_is_one_undo_step_and_undo_and_redo_are_exact():
    original = flow_xml(("A", [("Socket", ""), (CK, "K1"), ("Escalate", "")]), eol="\r\n", unit="\t").encode("utf-8")
    doc = PcsDieFileDoc(original, "r.xml")
    text0 = doc.current_text()
    doc.set_flow("xofy", "x0", [(CK, "K2"), ("Escalate", "")])
    text1 = doc.current_text()
    assert text1 != text0 and doc.dirty
    undone = doc.undo()
    assert doc.current_text() == text0 and doc.export_bytes() == original and not doc.can_undo  # one step, not a byte off
    assert cells(undone.updates) == {
        ("x0", "action", "Socket; ConfigKey; Escalate"),
        ("x0", "alarm_config_key", "; K1; "),
        ("x0", "bin_or_token", "B"),
    }
    assert doc.redo() is not None and doc.current_text() == text1
    doc.set_flow("xofy", "x0", [("Socket", ""), (CK, "K1"), ("Escalate", "")])  # and back to what the file had
    assert doc.undo() is not None and doc.current_text() == text1 and doc.undo() is not None and doc.current_text() == text0


def test_a_flow_and_a_cell_edit_undo_one_after_the_other():
    from pcs_die_file.model import CellEdit

    doc = flow_doc(("A", [("Socket", ""), ("Escalate", "")]))
    doc.set_flow("xofy", "x0", [("Socket", ""), (CK, "K1"), ("Escalate", "")])
    doc.apply_edits([CellEdit("xofy", "x0", "bins", "FB7002")])
    assert doc.undo() is not None and doc.rows("xofy")[0]["bins"] == "FB7001" and len(doc.flow("xofy", "x0")) == 3
    assert doc.undo() is not None and doc.flow("xofy", "x0") == [("Socket", ""), ("Escalate", "")]


def test_a_key_with_a_semicolon_is_one_key():
    doc = flow_doc(("A", [("Socket", "")]))
    assert doc.set_flow("xofy", "x0", [(CK, "A;B")]).applied == 1
    assert doc.flow("xofy", "x0") == [(CK, "A;B")]  # (not two alarms: no ';' list is parsed)


def test_a_new_alarm_never_keeps_the_key_of_the_alarm_it_was_copied_from():
    doc = flow_doc(("A", [("Socket", ""), (CK, "K1")]))
    doc.set_flow("xofy", "x0", [("Socket", ""), (CK, "K1"), ("Escalate", "")])  # the copy of the last alarm had K1
    assert doc.flow("xofy", "x0") == [("Socket", ""), (CK, "K1"), ("Escalate", "")]


def test_an_alarm_without_an_action_stays_without_one_when_the_flow_is_written():
    xml = flow_xml(("A", [("X", "K"), ("Socket", "")])).replace(' Action="X"', "")  # a Message alarm has no Action
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    assert doc.flow("xofy", "x0") == [("", "K"), ("Socket", "")]
    got = fl.apply_pick(doc.flow("xofy", "x0"), "action", [["Socket", 1], ["", 0], ["Escalate", None]], "HST")
    assert doc.set_flow("xofy", "x0", got.steps).applied == 1
    root = etree.fromstring(doc.current_text().encode("utf-8"))
    assert [a.get("Action") for a in root.iter("Alarm")] == ["Socket", None, "Escalate"]
    assert [a.get("ConfigKey") for a in root.iter("Alarm")] == [None, "K", None]


def test_a_failed_write_leaves_the_rule_as_it_was(monkeypatch):
    original = flow_xml(("A", [("Socket", ""), (CK, "K1")])).encode("utf-8")
    doc = PcsDieFileDoc(original, "r.xml")
    text0, real, calls = doc.current_text(), PcsDieFileDoc._set_attr, []

    def flaky(self, el, name, value):
        calls.append(name)
        if len(calls) == 3:
            raise ValueError("boom")
        real(self, el, name, value)

    monkeypatch.setattr(PcsDieFileDoc, "_set_attr", flaky)
    res = doc.set_flow("xofy", "x0", [(CK, "K2"), ("Socket", ""), ("Escalate", "")])
    assert (res.applied, res.errors) == (0, ["boom"])
    assert doc.current_text() == text0 and not doc.can_undo and not doc.dirty and doc.export_bytes() == original


def test_an_empty_flow_a_missing_rule_and_a_rule_without_alarms_are_refused():
    doc = flow_doc(("A", [("Socket", "")]))
    assert doc.set_flow("xofy", "x0", []).errors == ["A rule needs at least one alarm."]
    assert doc.set_flow("xofy", "x9", [("Socket", "")]).skipped_unknown == 1
    assert doc.set_flow("workflow", "w0", [("Socket", "")]).skipped_unknown == 1
    bare = PcsDieFileDoc(flow_xml(("A", [("Socket", "")])).replace("<Alarm ", "<Other ").encode("utf-8"), "r.xml")
    assert bare.set_flow("xofy", "x0", [("Socket", "")]).skipped_readonly == 1
    assert not doc.can_undo and not bare.can_undo


def test_alarms_that_are_not_all_in_one_place_keep_their_number():
    xml = flow_xml(("A", [("Socket", ""), ("Escalate", "")]))
    xml = xml.replace('<RepeatTriggerGroup RepeatTriggerHistoryLength="3">', '<Alarm Order="0" Action="Reboot" />\n<RepeatTriggerGroup>')
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    assert [a for a, _ in doc.flow("xofy", "x0")] == ["Reboot", "Socket", "Escalate"]
    refused = doc.set_flow("xofy", "x0", [("Reboot", ""), ("Socket", "")])
    assert refused.applied == 0 and "not all in one place" in refused.errors[0]
    assert doc.set_flow("xofy", "x0", [("AP", ""), ("TIU", ""), ("Tester", "")]).applied == 1  # same number: fine
    assert [a for a, _ in doc.flow("xofy", "x0")] == ["AP", "TIU", "Tester"]


def test_a_pick_goes_from_the_picker_to_the_rule():
    doc = flow_doc(("A", HST_FLOW))
    known = {"actions": fl.actions_of(ALARM_MAP), "keys": doc.workflow_keys()}
    got = fl.apply_pick(doc.flow("xofy", "x0"), "action", [[CK, 1], ["Socket", 0], [CK, None], ["Escalate", 2]], "HST", **known)
    assert doc.set_flow("xofy", "x0", got.steps).applied == 1
    got = fl.apply_pick(doc.flow("xofy", "x0"), "alarm_config_key", ["", "K2"], "HST", **known)
    assert doc.set_flow("xofy", "x0", got.steps).applied == 1
    assert doc.flow("xofy", "x0") == [(CK, ""), ("Socket", ""), (CK, "K2"), ("Escalate", "")]
    assert doc.rows("xofy")[0]["action"] == "ConfigKey; Socket; ConfigKey; Escalate"
    assert doc.rows("xofy")[0]["alarm_config_key"] == "; ; K2; "


# ----------------------------------------------------------------------------------------- the real pcs_die_files
@needs_emr
def test_the_check_dut_orientation_rule_of_the_hst_file_keeps_its_key_with_the_configkey_action():
    original = EMR.read_bytes()
    doc = PcsDieFileDoc(original, EMR.name)
    row = next(r for r in doc.rows("xofy") if r["rule_name"] == "XofY_Bin7906_FAILED_CHECKDUTORIENTATION")
    assert row["_flow"] == [list(step) for step in HST_FLOW]
    assert row["alarm_config_key"] == "; Check_DUT_Orientation; "
    assert "Check_DUT_Orientation" in doc.workflow_keys()
    got = fl.apply_pick(doc.flow("xofy", row["id"]), "action", [["Socket", 0], ["Socket", None], [CK, 1], ["Escalate", 2]], "HST",
                        actions=fl.actions_of(ALARM_MAP), keys=doc.workflow_keys())
    assert doc.set_flow("xofy", row["id"], got.steps).applied == 1
    after = next(r for r in doc.rows("xofy") if r["id"] == row["id"])
    assert after["action"] == "Socket; Socket; ConfigKey; Escalate" and after["alarm_config_key"] == "; ; Check_DUT_Orientation; "
    out = doc.export_bytes()
    assert doc.export_mode == "patch" and out.count(b"<Alarm ") == original.count(b"<Alarm ") + 1  # one alarm more, nothing else new
    assert doc.undo() is not None and doc.export_bytes() == original  # and not a byte of the file is different after undo


@needs_sst
def test_the_sst_file_has_the_same_key_more_than_once_in_a_rule_and_every_action_is_configkey():
    original = SST.read_bytes()
    doc = PcsDieFileDoc(original, SST.name)
    repeated = [r for r in doc.rows("xofy") if len({k for _, k in r["_flow"]}) < len(r["_flow"])]
    assert len(repeated) > 100  # (174 of the 197 rules)
    row = repeated[0]
    assert {a for a, _ in row["_flow"]} == {CK} and row["action"] == CK  # all alarms read "ConfigKey"
    keys = doc.workflow_keys()
    assert len(keys) == 10 and all(k in keys for _, k in row["_flow"])
    got = fl.apply_pick(doc.flow("xofy", row["id"]), "alarm_config_key", [keys[0], keys[0], keys[1], keys[2]], "SST",
                        actions=fl.actions_of(ALARM_MAP), keys=keys)
    assert got.replaced == () and doc.set_flow("xofy", row["id"], got.steps).applied == 1
    after = next(r for r in doc.rows("xofy") if r["id"] == row["id"])
    assert after["_flow"] == [[CK, keys[0]], [CK, keys[0]], [CK, keys[1]], [CK, keys[2]]]
    assert doc.export_bytes().count(b"<Alarm ") == original.count(b"<Alarm ") + 1 and doc.export_mode == "patch"
    assert doc.undo() is not None and doc.export_bytes() == original


@needs_sst
def test_the_odd_rule_of_the_sst_file_is_told_before_its_action_is_replaced():
    doc = PcsDieFileDoc(SST.read_bytes(), SST.name)
    odd = next(r for r in doc.rows("xofy") if r["_flow"][0][0] == "Abc")  # Action="Abc" with a ConfigKey
    got = fl.apply_pick(doc.flow("xofy", odd["id"]), "alarm_config_key", [k for _, k in odd["_flow"] if k], "SST")
    assert got.replaced == ("Abc",) and all(a == CK for a, _ in got.steps)
