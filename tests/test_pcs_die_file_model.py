from __future__ import annotations

import pytest
from lxml import etree

from pcs_die_file.model import COLUMNS, READ_ONLY_FIELDS, CellEdit, PcsDieFileDoc

P1 = r"M:\pcs\wf\SST_PowerCycle.xml"
P2 = r"M:\pcs\wf\SST_ReplaceMB.xml"

PCS_DIE_FILE = rf"""<ComponentRecipe checksum="x">
  <Identity type="iLLIAD" datagroup="SST_X" name="Die" id="1" version="1.0" asof="2024-01-01T00:00:00" />
  <SharedConfigurations>
    <ConfigGroup Key="PowerCycle">
      <Workflow Path="{P1}" />
    </ConfigGroup>
    <ConfigGroup Key="ReplaceMB">
      <Workflow Path="{P2}" />
    </ConfigGroup>
    <ConfigGroup Key="PowerCycleAgain">
      <Workflow Path="  {P1}  " />
    </ConfigGroup>
    <ConfigGroup Key="NoWorkflow" />
    <ConfigGroup Key="Blank">
      <Workflow Path="   " />
    </ConfigGroup>
  </SharedConfigurations>
  <RuleGroup GroupName="Parametric">
    <PARAMETRIC ParameterName="VCTIMCount" UpperLimit="6500">
      <Alarm ConfigKey="SST_ReplaceVCTIM" />
    </PARAMETRIC>
  </RuleGroup>
  <RuleGroup GroupName="XofYBinGroup" ValidOperations="">
    <XOFYBIN Bins="FB700" X="4" Y="5" RuleName="6900">
      <RepeatTriggerGroup>
        <Alarm Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="7000" Action="ConfigKey" ConfigKey="SST_ReplaceMB" />
      </RepeatTriggerGroup>
    </XOFYBIN>
  </RuleGroup>
</ComponentRecipe>
"""


@pytest.fixture
def doc() -> PcsDieFileDoc:
    return PcsDieFileDoc(PCS_DIE_FILE.encode("utf-8"), "pcs_die_file.xml")


def checks(doc: PcsDieFileDoc) -> dict[str, str]:
    return {row["id"]: row["check"] for row in doc.rows("workflow")}


def cells(updates) -> set[tuple[str, str, str]]:
    return {(u.row_id, u.field, u.value) for u in updates}


def test_check_is_a_workflow_column_only():
    assert COLUMNS["workflow"] == [("key", "Key"), ("path", "Path"), ("check", "Check")]
    assert all(f != "check" for f, _ in COLUMNS["parametric"] + COLUMNS["xofy"])
    assert "check" in READ_ONLY_FIELDS


def test_rows_start_without_a_verdict(doc):
    assert set(checks(doc).values()) == {""}
    assert [row["path"] for row in doc.rows("workflow")][:2] == [P1, P2]


def test_workflow_paths_are_distinct_stripped_and_in_file_order(doc):
    assert doc.workflow_paths() == [P1, P2]  # the padded duplicate, the missing Workflow and the blank path are out


def test_a_verdict_lands_on_every_row_with_that_path(doc):
    updates = doc.set_path_checks({P1: "OK (1.0 KB)"})
    assert cells(updates) == {("w0", "check", "OK (1.0 KB)"), ("w2", "check", "OK (1.0 KB)")}
    assert checks(doc) == {"w0": "OK (1.0 KB)", "w1": "", "w2": "OK (1.0 KB)", "w3": "", "w4": ""}


def test_only_changed_cells_are_reported(doc):
    doc.set_path_checks({P1: "Queued", P2: "Queued"})
    updates = doc.set_path_checks({P1: "OK", P2: "Queued"})
    assert cells(updates) == {("w0", "check", "OK"), ("w2", "check", "OK")}
    assert doc.set_path_checks({P1: "OK", P2: "Queued"}) == []


def test_clearing(doc):
    doc.set_path_checks({P1: "OK", P2: "Not found"})
    assert len(doc.set_path_checks(None)) == 3
    assert set(checks(doc).values()) == {""}
    assert doc.set_path_checks({}) == []


def test_blank_keys_are_ignored(doc):
    assert doc.set_path_checks({"": "OK", "   ": "OK"}) == []
    assert doc.workflow_paths() == [P1, P2]


def test_verdicts_are_keyed_by_the_stripped_path(doc):
    doc.set_path_checks({f"  {P2}\t": "Not found"})
    assert checks(doc)["w1"] == "Not found"


def test_the_check_cell_follows_an_edited_path(doc):
    doc.set_path_checks({P1: "OK", P2: "Not found"})
    res = doc.apply_edits([CellEdit("workflow", "w1", "path", P1)])
    assert res.applied == 1
    assert ("w1", "path", P1) in cells(res.updates) and ("w1", "check", "OK") in cells(res.updates)

    res = doc.apply_edits([CellEdit("workflow", "w1", "path", r"M:\pcs\wf\Other.xml")])
    assert ("w1", "check", "") in cells(res.updates)  # nobody has looked at this path yet

    undone = doc.undo()
    assert ("w1", "check", "OK") in cells(undone.updates)
    undone = doc.undo()
    assert ("w1", "check", "Not found") in cells(undone.updates)
    redone = doc.redo()
    assert ("w1", "check", "OK") in cells(redone.updates)


def test_check_cannot_be_edited(doc):
    doc.set_path_checks({P1: "OK"})
    res = doc.apply_edits([CellEdit("workflow", "w0", "check", "hacked")])
    assert (res.applied, res.skipped_readonly) == (0, 1)
    assert checks(doc)["w0"] == "OK"
    assert not doc.is_editable("workflow", "w0", "check")
    assert doc.is_editable("workflow", "w0", "path")
    assert not doc.dirty and not doc.can_undo


def test_a_verdict_is_not_an_edit(doc):
    revision = doc.revision
    doc.set_path_checks({P1: "OK", P2: "Not found"})
    assert doc.revision == revision and not doc.dirty and not doc.can_undo
    assert doc.export_bytes() == PCS_DIE_FILE.encode("utf-8")  # nothing of it reaches the file


def test_export_ignores_the_check_column(doc):
    doc.set_path_checks({P1: "OK (1.0 KB)", P2: "Not found"})
    doc.apply_edits([CellEdit("workflow", "w0", "key", "PowerCycle1")])
    out = doc.export_bytes().decode("utf-8")
    assert out == PCS_DIE_FILE.replace('Key="PowerCycle"', 'Key="PowerCycle1"')


def test_added_row_has_an_empty_check_and_a_blank_path_is_never_sent(doc):
    doc.set_path_checks({P1: "OK"})
    res = doc.add_row("workflow", "w1")
    row = res.added[0].row
    assert row["check"] == "" and row["path"] == ""
    assert doc.workflow_paths() == [P1, P2]
    doc.apply_edits([CellEdit("workflow", row["id"], "path", P1)])
    assert checks(doc)[row["id"]] == "OK"  # a copy of a checked path shows that path's verdict at once


def test_deleted_rows_drop_out_of_the_paths(doc):
    doc.delete_row("workflow", "w1")
    assert doc.workflow_paths() == [P1]
    assert len(doc.set_path_checks({P2: "Not found"})) == 0  # no row shows P2 any more
    doc.undo()
    assert checks(doc)["w1"] == "Not found"  # ... and when the row comes back it shows the verdict again


def test_other_tables_are_untouched(doc):
    before = (doc.rows("parametric"), doc.rows("xofy"))
    doc.set_path_checks({P1: "OK"})
    assert (doc.rows("parametric"), doc.rows("xofy")) == before


# ---------------------------------------------------------------------------------------------- the Note column
def rules_xml(*rules: tuple[str, str]) -> str:
    """A pcs_die_file with one XOFYBIN rule per (RuleName, Bins)."""
    body = "".join(
        f"""    <XOFYBIN Bins="{bins}" X="4" Y="5" RuleName="{name}">
      <RepeatTriggerGroup>
        <Alarm Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="7000" Action="ConfigKey" ConfigKey="K" />
      </RepeatTriggerGroup>
    </XOFYBIN>
"""
        for name, bins in rules
    )
    return f"""<ComponentRecipe checksum="x">
  <Identity type="iLLIAD" datagroup="SST_X" name="Die" id="1" version="1.0" asof="2024-01-01T00:00:00" />
  <SharedConfigurations />
  <RuleGroup GroupName="XofYBinGroup" ValidOperations="">
{body}  </RuleGroup>
</ComponentRecipe>
"""


def rules_doc(*rules: tuple[str, str]) -> PcsDieFileDoc:
    return PcsDieFileDoc(rules_xml(*rules).encode("utf-8"), "r.xml")


def notes(doc: PcsDieFileDoc) -> list[str]:
    return [row["note"] for row in doc.rows("xofy")]


def test_note_is_a_read_only_column_of_the_rule_table_only():
    assert COLUMNS["xofy"][-2:] == [("error", "Validation Error"), ("note", "Note")]
    assert "note" in READ_ONLY_FIELDS
    assert all(f != "note" for f, _ in COLUMNS["workflow"] + COLUMNS["parametric"])


def test_bin_rules_are_id_name_bins_and_valid_operations_in_file_order():
    assert rules_doc(("A", "IB70,[FB7056]"), ("B", "FB7006")).bin_rules() == [
        ("x0", "A", "IB70,[FB7056]", ""),
        ("x1", "B", "FB7006", ""),
    ]
    assert PcsDieFileDoc(PCS_DIE_FILE.encode("utf-8")).bin_rules() == [("x0", "6900", "FB700", "")]


def test_bin_rules_carry_the_valid_operations_of_their_rule_group():
    doc = rules_doc(("A", "IB70"))
    doc.apply_edits([CellEdit("xofy", "x0", "valid_operations", "VPO")])
    assert doc.bin_rules() == [("x0", "A", "IB70", "VPO")]


def test_bin_rules_read_the_rule_name_where_the_file_keeps_it():
    """The real die file has Bins on the XOFYBIN and RuleName on its RepeatTriggerGroup."""
    xml = rules_xml(("A", "IB70")).replace(' RuleName="A">', ">", 1).replace("<RepeatTriggerGroup>", '<RepeatTriggerGroup RuleName="A">')
    assert '<XOFYBIN Bins="IB70" X="4" Y="5">' in xml and xml.count('RuleName="A"') == 1  # (it moved)
    assert PcsDieFileDoc(xml.encode("utf-8")).bin_rules() == [("x0", "A", "IB70", "")]


def test_bin_rules_follow_the_edits():
    doc = rules_doc(("A", "IB70"))
    doc.apply_edits([CellEdit("xofy", "x0", "bins", "FB7006,[FB7056]"), CellEdit("xofy", "x0", "rule_name", "A2")])
    assert doc.bin_rules() == [("x0", "A2", "FB7006,[FB7056]", "")]


def test_notes_land_on_their_rows_and_only_changed_cells_are_reported():
    doc = rules_doc(("A", "IB70"), ("B", "FB7006"))
    assert notes(doc) == ["", ""]
    updates = doc.set_validation({}, {"x1": "Duplicate of A"})
    assert cells(updates) == {("x0", "error", "OK"), ("x1", "error", "OK"), ("x1", "note", "Duplicate of A")}
    assert notes(doc) == ["", "Duplicate of A"]
    assert doc.set_validation({}, {"x1": "Duplicate of A"}) == []  # nothing changed, nothing to send
    assert cells(doc.set_validation({}, {"x0": "Duplicate of B"})) == {("x1", "note", ""), ("x0", "note", "Duplicate of B")}
    assert cells(doc.set_validation({}, None)) == {("x0", "note", "")}
    assert notes(doc) == ["", ""]


def test_empty_notes_and_notes_of_rows_that_do_not_exist_are_dropped():
    doc = rules_doc(("A", "IB70"))
    doc.set_validation({}, {"x0": "", "x9": "Duplicate of Z", "w0": "x"})
    assert notes(doc) == [""]


def test_a_note_is_not_an_edit_and_cannot_be_edited():
    xml = rules_xml(("A", "IB70"), ("B", "FB7006"))
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    revision = doc.revision
    doc.set_validation({}, {"x1": "Duplicate of A"})
    assert doc.revision == revision and not doc.dirty and not doc.can_undo
    res = doc.apply_edits([CellEdit("xofy", "x1", "note", "hacked")])
    assert (res.applied, res.skipped_readonly) == (0, 1) and notes(doc) == ["", "Duplicate of A"]
    assert not doc.is_editable("xofy", "x1", "note") and doc.is_editable("xofy", "x1", "bins")
    assert doc.export_bytes() == xml.encode("utf-8")  # nothing of it reaches the file


def test_a_deleted_row_takes_its_note_along_and_undo_brings_it_back():
    doc = rules_doc(("A", "IB70"), ("B", "FB7006"))
    doc.set_validation({}, {"x1": "Duplicate of A"})
    doc.delete_row("xofy", "x1")
    assert notes(doc) == [""]
    assert cells(doc.set_validation({}, {"x1": "stale"})) == set()  # a row that is gone has nothing to show
    (added,) = doc.undo().added
    assert added.row["note"] == "Duplicate of A" and added.row["error"] == "OK"  # the row is back as it was
    assert notes(doc) == ["", "Duplicate of A"]


def test_clearing_the_validation_clears_the_notes_too():
    doc = rules_doc(("A", "IB70"), ("B", "FB7006"))
    doc.set_validation({}, {"x1": "Duplicate of A"})
    updates = doc.set_validation({}, {"x1": "Duplicate of A"}, checked=False)
    assert cells(updates) == {("x0", "error", ""), ("x1", "error", ""), ("x1", "note", "")}


def test_an_added_row_has_no_note():
    doc = rules_doc(("A", "IB70"))
    doc.set_validation({}, {"x0": "Duplicate of Z"})
    assert doc.add_row("xofy", "x0").added[0].row["note"] == ""


# ----------------------------------------------------------------------------------------- the alarm columns
LAYOUTS = [("\n", "  "), ("\r\n", "\t"), ("", "")]  # LF + spaces, CRLF + tabs, everything on one line


def alarm_xml(*rules, eol: str = "\n", unit: str = "  ") -> str:
    """XOFYBIN rules in the layout of the real files: (RuleName, alarms) with one <Alarm> per alarm in a single
    <RepeatTriggerGroup>, numbered with Order.  An alarm is its Action, or (Action, BinOrToken)."""

    def alarm(i: int, spec) -> str:
        action, token = (spec, "B") if isinstance(spec, str) else spec
        return f'<Alarm Order="{i}" Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="{token}" Action="{action}" />'

    lines = [
        '<ComponentRecipe checksum="x">',
        f'{unit}<Identity type="iLLIAD" datagroup="SST_X" name="Die" id="1" version="1.0" asof="2024-01-01T00:00:00" />',
        f"{unit}<SharedConfigurations />",
        f'{unit}<RuleGroup GroupName="XofYBinGroup" ValidOperations="">',
    ]
    for name, alarms in rules:
        lines.append(f'{unit * 2}<XOFYBIN Bins="FB7001" X="4" Y="5" RuleName="{name}">')
        lines.append(f'{unit * 3}<RepeatTriggerGroup RepeatTriggerHistoryLength="3">')
        lines += [f"{unit * 4}{alarm(i, spec)}" for i, spec in enumerate(alarms, 1)]
        lines += [f"{unit * 3}</RepeatTriggerGroup>", f"{unit * 2}</XOFYBIN>"]
    lines += [f"{unit}</RuleGroup>", "</ComponentRecipe>"]
    return eol.join(lines) + eol


def alarm_doc(*rules, **layout) -> PcsDieFileDoc:
    return PcsDieFileDoc(alarm_xml(*rules, **layout).encode("utf-8"), "r.xml")


def alarms_of(doc: PcsDieFileDoc, rule: int = 0) -> list[tuple[str | None, str | None]]:
    """(Order, Action) of the alarms of the n-th XOFYBIN rule, as the tree has them now."""
    root = etree.fromstring(doc.current_text().encode("utf-8"))
    return [(a.get("Order"), a.get("Action")) for a in root.findall(".//XOFYBIN")[rule].iter("Alarm")]


@pytest.mark.parametrize(("eol", "unit"), LAYOUTS)
def test_a_longer_list_gives_the_rule_more_alarms(eol, unit):
    doc = alarm_doc(("A", ["Socket", "Escalate"]), eol=eol, unit=unit)
    res = doc.apply_edits([CellEdit("xofy", "x0", "action", "PowerCycle; Socket; Escalate")])
    assert (res.applied, res.errors) == (1, [])
    assert alarms_of(doc) == [("1", "PowerCycle"), ("2", "Socket"), ("3", "Escalate")]  # the new alarm goes on counting
    assert doc.rows("xofy")[0]["action"] == "PowerCycle; Socket; Escalate"
    expected = alarm_xml(("A", ["PowerCycle", "Socket", "Escalate"]), eol=eol, unit=unit).encode("utf-8")
    assert doc.export_bytes() == expected and doc.export_mode == "patch"  # edited in place, laid out like the others


@pytest.mark.parametrize(("eol", "unit"), LAYOUTS)
def test_a_shorter_list_takes_the_last_alarms_out(eol, unit):
    doc = alarm_doc(("A", ["PowerCycle", "Socket", "Escalate"]), eol=eol, unit=unit)
    doc.apply_edits([CellEdit("xofy", "x0", "action", "Socket;Escalate")])
    assert alarms_of(doc) == [("1", "Socket"), ("2", "Escalate")]
    assert doc.rows("xofy")[0]["action"] == "Socket; Escalate"
    assert doc.export_bytes() == alarm_xml(("A", ["Socket", "Escalate"]), eol=eol, unit=unit).encode("utf-8")
    assert doc.export_mode == "patch"


def test_a_list_as_long_as_the_alarms_changes_no_alarm():
    doc = alarm_doc(("A", ["Socket", "Escalate"]))
    doc.apply_edits([CellEdit("xofy", "x0", "action", "Tester; Reboot")])
    assert alarms_of(doc) == [("1", "Tester"), ("2", "Reboot")]


def test_a_value_without_a_semicolon_still_goes_to_every_alarm():
    doc = alarm_doc(("A", ["PowerCycle", "Socket", "Escalate"]))
    doc.apply_edits([CellEdit("xofy", "x0", "action", "Escalate")])
    assert alarms_of(doc) == [("1", "Escalate"), ("2", "Escalate"), ("3", "Escalate")]
    assert doc.rows("xofy")[0]["action"] == "Escalate"


def test_a_list_in_a_rule_column_is_no_list_of_alarms():
    doc = alarm_doc(("A", ["Socket", "Escalate"]))
    doc.apply_edits([CellEdit("xofy", "x0", "bins", "FB7001; FB7002; FB7003")])
    assert len(alarms_of(doc)) == 2 and doc.rows("xofy")[0]["bins"] == "FB7001; FB7002; FB7003"  # (the checker judges it)


def test_binortoken_and_configkey_lists_resize_too():
    doc = alarm_doc(("A", ["Socket"]))
    doc.apply_edits([CellEdit("xofy", "x0", "bin_or_token", "B1; B2; B3")])
    assert alarms_of(doc) == [("1", "Socket"), ("2", "Socket"), ("3", "Socket")]  # the new ones copy the last alarm
    assert doc.rows("xofy")[0]["bin_or_token"] == "B1; B2; B3"
    doc.apply_edits([CellEdit("xofy", "x0", "alarm_config_key", "K1; ; K3")])
    assert doc.rows("xofy")[0]["alarm_config_key"] == "K1; ; K3"
    assert doc.apply_edits([CellEdit("xofy", "x0", "alarm_config_key", "K1;")]).applied == 1
    assert len(alarms_of(doc)) == 2 and doc.rows("xofy")[0]["alarm_config_key"] == "K1; "  # one value per alarm: two left


def test_the_cells_made_of_the_alarms_are_sent_again_after_a_resize():
    doc = alarm_doc(("A", [("Socket", "B1"), ("Escalate", "B2")]))
    res = doc.apply_edits([CellEdit("xofy", "x0", "action", "PowerCycle; Socket; Escalate")])
    assert cells(res.updates) == {
        ("x0", "action", "PowerCycle; Socket; Escalate"),
        ("x0", "bin_or_token", "B1; B2; B2"),  # the new alarm copies the last one
        ("x0", "alarm_config_key", ""),
    }  # what is not made of the alarms (Bins, X, RuleName ...) is not sent
    undone = doc.undo()
    assert cells(undone.updates) == {
        ("x0", "action", "Socket; Escalate"),
        ("x0", "bin_or_token", "B1; B2"),
        ("x0", "alarm_config_key", ""),
    }


def test_undo_and_redo_of_a_resize_are_exact():
    original = alarm_xml(("A", ["Socket", "Escalate"]), eol="\r\n", unit="\t").encode("utf-8")
    doc = PcsDieFileDoc(original, "r.xml")
    text0 = doc.current_text()
    doc.apply_edits([CellEdit("xofy", "x0", "action", "PowerCycle; Socket; Escalate")])
    text1 = doc.current_text()
    assert text1 != text0 and doc.undo() is not None
    assert doc.current_text() == text0 and doc.export_bytes() == original  # not a space is different
    assert doc.redo() is not None and doc.current_text() == text1
    doc.apply_edits([CellEdit("xofy", "x0", "action", "Socket; Escalate")])  # and back to two alarms
    text2 = doc.current_text()
    assert doc.undo() is not None and doc.current_text() == text1
    assert doc.undo() is not None and doc.current_text() == text0
    assert doc.redo() is not None and doc.redo() is not None and doc.current_text() == text2


def test_a_cell_paste_of_several_columns_is_one_undo_step():
    doc = alarm_doc(("A", ["Socket", "Escalate"]))
    doc.apply_edits([
        CellEdit("xofy", "x0", "action", "PowerCycle; Socket; Escalate"),
        CellEdit("xofy", "x0", "bin_or_token", "B1; B2; B3"),  # (the alarms are three by now)
    ])
    assert len(alarms_of(doc)) == 3
    doc.undo()
    assert alarms_of(doc) == [("1", "Socket"), ("2", "Escalate")] and not doc.can_undo


def test_a_failed_edit_leaves_the_alarms_as_they_were(monkeypatch):
    original = alarm_xml(("A", ["Socket", "Escalate"])).encode("utf-8")
    doc = PcsDieFileDoc(original, "r.xml")
    text0, real, calls = doc.current_text(), PcsDieFileDoc._set_attr, []

    def flaky(self, el, name, value):
        calls.append(name)
        if len(calls) == 2:
            raise ValueError("boom")
        real(self, el, name, value)

    monkeypatch.setattr(PcsDieFileDoc, "_set_attr", flaky)
    res = doc.apply_edits([CellEdit("xofy", "x0", "action", "PowerCycle; Socket; Escalate")])
    assert (res.applied, res.errors) == (0, ["boom"])
    assert doc.current_text() == text0 and not doc.can_undo and doc.export_bytes() == original


def test_the_alarms_of_a_rule_without_a_repeat_trigger_group_get_no_order():
    xml = alarm_xml(("A", ["Socket", "Escalate"])).replace("<RepeatTriggerGroup RepeatTriggerHistoryLength=\"3\">", "")
    xml = xml.replace("</RepeatTriggerGroup>", "").replace('Order="1" ', "").replace('Order="2" ', "")
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    doc.apply_edits([CellEdit("xofy", "x0", "action", "A; B; C")])
    assert alarms_of(doc) == [(None, "A"), (None, "B"), (None, "C")]
    out = doc.export_bytes().decode("utf-8")
    assert doc.export_mode == "patch" and out.count("<Alarm ") == 3 and "Order" not in out


COMMENTED = """<ComponentRecipe checksum="x">
  <Identity type="iLLIAD" datagroup="SST_X" name="Die" id="1" version="1.0" asof="2024-01-01T00:00:00" />
  <SharedConfigurations />
  <RuleGroup GroupName="XofYBinGroup" ValidOperations="">
    <XOFYBIN Bins="FB7001" X="4" Y="5" RuleName="A">
      <RepeatTriggerGroup RepeatTriggerHistoryLength="3">
        <Alarm Order="1" Rule="XofY" BinOrToken="B" Action="PowerCycle" />
        <Alarm Order="2" Rule="XofY" BinOrToken="B" Action="Socket" />
        <!-- last resort -->
        <Alarm Order="3" Rule="XofY" BinOrToken="B" Action="Escalate" />
        <!-- end of the steps -->
      </RepeatTriggerGroup>
    </XOFYBIN>
  </RuleGroup>
</ComponentRecipe>
"""


def test_comments_between_the_alarms_stay_when_alarms_go_or_come():
    doc = PcsDieFileDoc(COMMENTED.encode("utf-8"), "r.xml")
    doc.apply_edits([CellEdit("xofy", "x0", "action", "Socket; Escalate")])
    expected = (
        COMMENTED.replace('Action="PowerCycle"', 'Action="Socket"', 1)
        .replace('Action="Socket" />\n        <!-- last', 'Action="Escalate" />\n        <!-- last', 1)
        .replace('        <Alarm Order="3" Rule="XofY" BinOrToken="B" Action="Escalate" />\n', "")
    )
    assert doc.export_bytes().decode("utf-8") == expected and doc.export_mode == "patch"
    doc.undo()
    assert doc.export_bytes() == COMMENTED.encode("utf-8")
    doc.apply_edits([CellEdit("xofy", "x0", "action", "A; B; C; D")])  # one more: right behind the last alarm
    out = doc.export_bytes().decode("utf-8")
    assert doc.export_mode == "patch"
    assert out.index('Action="D"') < out.index("<!-- end of the steps -->") and out.index("<!-- last resort -->") < out.index('Action="C"')


def test_a_new_row_takes_a_list_longer_than_the_alarms_it_was_copied_from():
    doc = alarm_doc(("A", ["Socket", "Escalate"]))
    row = doc.add_row("xofy", "x0").added[0].row["id"]
    assert doc.rows("xofy")[1]["action"] == "Socket; Escalate"  # (the copy has the two alarms of the rule above)
    doc.apply_edits([CellEdit("xofy", row, "action", "PowerCycle; Socket; Escalate")])
    assert alarms_of(doc, 1) == [("1", "PowerCycle"), ("2", "Socket"), ("3", "Escalate")]
    assert alarms_of(doc, 0) == [("1", "Socket"), ("2", "Escalate")]  # the rule it was copied from is not touched
    assert doc.export_bytes().decode("utf-8").count("<Alarm ") == 5 and doc.export_mode == "patch"
    doc.undo()
    assert alarms_of(doc, 1) == [("1", "Socket"), ("2", "Escalate")]
    doc.redo()
    assert alarms_of(doc, 1) == [("1", "PowerCycle"), ("2", "Socket"), ("3", "Escalate")]
    doc.delete_row("xofy", row)
    doc.undo()  # the row is back with its three alarms
    assert alarms_of(doc, 1) == [("1", "PowerCycle"), ("2", "Socket"), ("3", "Escalate")]


def test_resizing_then_deleting_the_row_then_undoing_everything_gives_the_file_back():
    original = alarm_xml(("A", ["Socket", "Escalate"]), ("B", ["PowerCycle", "Escalate"])).encode("utf-8")
    doc = PcsDieFileDoc(original, "r.xml")
    doc.apply_edits([CellEdit("xofy", "x0", "action", "PowerCycle; Socket; Escalate")])
    doc.apply_edits([CellEdit("xofy", "x1", "action", "Socket; Reboot; Escalate; Tester")])
    doc.delete_row("xofy", "x1")
    assert doc.export_bytes().decode("utf-8").count("<Alarm ") == 3 and doc.export_mode == "patch"
    while doc.can_undo:
        doc.undo()
    assert doc.export_bytes() == original
