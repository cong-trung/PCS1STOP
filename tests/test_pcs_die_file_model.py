from __future__ import annotations

import pytest

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
