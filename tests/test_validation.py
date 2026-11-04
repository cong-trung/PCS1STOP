from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from config import DEFAULT_CONFIG_PATH, Settings
from pcs_die_file import validation as rv
from pcs_die_file.model import CellEdit, PcsDieFileDoc

REAL_DIE_FILE = Path(__file__).with_name("EMR_MCC_Die.xml")

PCS_DIE_FILE = """<ComponentRecipe checksum="x">
  <Identity type="iLLIAD" datagroup="SST_X" name="Die" id="1" version="1.0" asof="2024-01-01T00:00:00" />
  <SharedConfigurations />
  <RuleGroup GroupName="XofYBinGroup" ValidOperations="">
    <XOFYBIN Bins="FB700" X="4" Y="5" RuleName="6900">
      <RepeatTriggerGroup>
        <Alarm Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="7000" Action="ConfigKey" ConfigKey="SST_ReplaceMB" />
      </RepeatTriggerGroup>
    </XOFYBIN>
  </RuleGroup>
</ComponentRecipe>
"""


def die_file(*rules: tuple[str, str]) -> str:
    """A pcs_die_file the checker has nothing to say about, with one XOFYBIN rule per (RuleName, Bins)."""
    body = "".join(
        f"""    <XOFYBIN Bins="{bins}" X="4" Y="5" RuleName="{name}" CollateralType="Tester">
      <RepeatTriggerGroup RepeatTriggerHistoryLength="3">
        <Alarm Order="1" Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="7000" Action="PowerCycle" />
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


def check_with_duplicates(xml: str) -> rv.CheckResult:
    """What the page does: the checker and the duplicate step on the same document state."""
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    settings = Settings(DEFAULT_CONFIG_PATH)
    return rv.run_check(doc.current_text(), "r.xml", settings.alarm_map, True, None, doc.bin_rules())


def test_validation_uses_the_alarm_map_that_ships_with_the_app():
    settings = Settings(DEFAULT_CONFIG_PATH)
    result = rv.run_check(PCS_DIE_FILE, "r.xml", settings.alarm_map, settings.skip_workflow_check)
    assert result.warnings == []  # the map was found (nothing outside the project folder is needed)


def test_a_missing_alarm_map_is_reported_not_fatal(tmp_path):
    result = rv.run_check(PCS_DIE_FILE, "r.xml", str(tmp_path / "nowhere" / "AlarmIdMapping.xml"), True)
    assert result.warnings and "Alarm map not found" in result.warnings[0]
    assert "[warn]" in result.text and "paths.alarm_map" in result.text


def test_no_alarm_map_path_at_all_is_reported_too():
    result = rv.run_check(PCS_DIE_FILE, "r.xml", "", True)
    assert result.warnings and "no path set" in result.warnings[0]


def test_the_shipped_settings_hold_no_absolute_paths():
    """What the app reads on start-up lives inside the project: no drive letter (C:\\...) in the shipped settings."""
    data = json.loads(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    for value in (data["paths"]["alarm_map"], data["database"]["path"]):
        assert not re.match(r"^[A-Za-z]:|^[/\\]", value), value


def _digit_errors(bins, exceptions=None):
    xml = PCS_DIE_FILE.replace("FB700", bins)
    res = rv.run_check(xml, "r.xml", Settings(DEFAULT_CONFIG_PATH).alarm_map, True, exceptions)
    return "digits after" in res.text


def test_bin_digit_rule():
    for ok in ("FB1234", "FB700", "IB12", "IB9", "DB12345678", "DB5000000", "FB1234,FB100"):
        assert not _digit_errors(ok), ok
    for bad in ("FB123", "FB12345", "IB123", "IB1", "DB1234567", "DB123456789", "FB1234,FB123"):
        assert _digit_errors(bad), bad


def test_bin_digit_exceptions_can_be_given():
    assert _digit_errors("FB123") and not _digit_errors("FB123", {"FB": ["123"]})
    assert _digit_errors("FB700", {"FB": []})

def test_a_rule_inside_another_rules_bins_is_a_warning_and_the_check_still_passes():
    res = check_with_duplicates(die_file(("Bin70xx", "IB70,[FB7056]"), ("Bin7006", "FB7006"), ("Bin7056", "FB7056")))
    assert res.ok and res.n_errors == 0 and res.per_rule == {}  # a warning is no error: the pcs_die_file can be saved
    assert res.n_warnings == 1 and [item.rule.name for item in res.duplicates] == ["Bin7006"]
    assert res.notes == {"x1": "Duplicate of Bin70xx"}  # the row of Bin7006
    assert res.text.startswith("No errors found")  # the Error tab says nothing about it
    assert res.warning_text.splitlines()[0] == "1 rule(s) lie completely inside the bins of another rule."
    for needle in ("Bin7006", "FB7006", "70060000-70069999", "Duplicate of:  Bin70xx", "IB70,[FB7056]",
                   "70000000-70559999;70570000-70999999"):
        assert needle in res.warning_text
    assert "Bin7056" not in res.warning_text  # the carve-out is no duplicate


def test_no_duplicates_no_warnings():
    res = check_with_duplicates(die_file(("Bin8805", "FB8805"), ("Bin7006", "FB7006")))
    assert res.ok and res.n_warnings == 0 and res.duplicates == [] and res.notes == {}
    assert res.warning_text == "No warnings found in pcs_die_file: r.xml"


def test_without_the_rules_the_duplicate_step_is_left_out():
    res = rv.run_check(die_file(("Bin70xx", "IB70"), ("Bin7006", "FB7006")), "r.xml", Settings(DEFAULT_CONFIG_PATH).alarm_map, True)
    assert res.ok and res.n_warnings == 0 and res.notes == {} and "No warnings" in res.warning_text


def test_a_warning_does_not_wait_for_an_error_to_be_fixed():
    xml = die_file(("Bin70xx", "IB70"), ("Bin7006", "FB7006")).replace('Y="5" RuleName="Bin7006"', 'RuleName="Bin7006"')
    res = check_with_duplicates(xml)
    assert not res.ok and "Bin7006" in res.per_rule  # Y is missing
    assert res.notes == {"x1": "Duplicate of Bin70xx"}


def test_a_note_names_three_rules_at_most_and_the_warning_tab_all_of_them():
    res = check_with_duplicates(die_file(*[(f"Big{i}", "IB70") for i in range(5)], ("Small", "FB7006")))
    assert res.notes["x5"] == "Duplicate of Big0, Big1, Big2 +2 more"
    assert "Duplicate of:  Big4" in res.warning_text


def test_a_crash_in_the_duplicate_step_does_not_stop_the_check(monkeypatch):
    def boom(rules):
        raise RuntimeError("boom")

    monkeypatch.setattr(rv.dup, "find_duplicates", boom)
    res = check_with_duplicates(die_file(("Bin70xx", "IB70"), ("Bin7006", "FB7006")))
    assert res.ok and res.n_warnings == 0 and res.notes == {}  # the checker's part is untouched
    assert res.warnings == ["Duplicate bins were not checked: RuntimeError: boom"]
    assert "[warn] Duplicate bins were not checked" in res.text


@pytest.mark.skipif(not REAL_DIE_FILE.is_file(), reason="tests/EMR_MCC_Die.xml is not part of this checkout")
def test_the_real_die_file_is_valid_and_has_warnings():
    path = REAL_DIE_FILE
    doc = PcsDieFileDoc(path.read_bytes(), path.name)
    settings = Settings(DEFAULT_CONFIG_PATH)
    res = rv.run_check(doc.current_text(), path.name, settings.alarm_map, settings.skip_workflow_check,
                       settings.bin_digit_exceptions, doc.bin_rules())
    assert res.ok  # (so it can be saved)
    assert res.n_warnings >= 1 and "Duplicate of XofY_Bin8803_BootHalt" in res.notes.values()
    assert "XofY_Bin88030218_EFIBoot" in res.warning_text


# ------------------------------------------------------------------------------- a list of actions in a new rule
def check_doc(doc: PcsDieFileDoc) -> rv.CheckResult:
    settings = Settings(DEFAULT_CONFIG_PATH)
    return rv.run_check(doc.current_text(), "r.xml", settings.alarm_map, True, None, doc.bin_rules())


def fill_new_rule(doc: PcsDieFileDoc, after: str, name: str, bins: str, action: str) -> str:
    """What the user does: Add row below ``after``, then type the identifying cells and the Action list."""
    row = doc.add_row("xofy", after).added[0].row["id"]
    res = doc.apply_edits([
        CellEdit("xofy", row, "rule_name", name),
        CellEdit("xofy", row, "bins", bins),
        CellEdit("xofy", row, "bin_or_token", name),
        CellEdit("xofy", row, "action", action),
    ])
    assert res.errors == []
    return row


def test_a_new_rule_takes_a_list_of_actions_longer_than_the_rule_it_was_copied_from():
    doc = PcsDieFileDoc(die_file(("Bin7000", "FB7000")).encode("utf-8"), "r.xml")  # the rule above has one alarm
    row = fill_new_rule(doc, "x0", "Bin8700", "FB8700", "PowerCycle; Socket; Escalate")
    assert doc.rows("xofy")[1]["action"] == "PowerCycle; Socket; Escalate"
    res = check_doc(doc)
    assert res.ok and res.n_errors == 0 and res.per_rule == {}, res.text  # (it used to say: ... attribute was not in the expected list of Action tokens)
    assert "Order attributes must not repeat" not in res.text
    out = doc.export_bytes().decode("utf-8")
    assert re.findall(r'<Alarm Order="(\d)"[^>]*BinOrToken="Bin8700" Action="(\w+)"', out) == [
        ("1", "PowerCycle"), ("2", "Socket"), ("3", "Escalate")]
    assert row == "x1"


def test_a_new_rule_takes_a_shorter_list_too():
    xml = die_file(("Bin7000", "FB7000")).replace(
        '<Alarm Order="1" Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="7000" Action="PowerCycle" />',
        "".join(f'<Alarm Order="{i}" Severity="ParameterControlWarning_3" Rule="XofY" BinOrToken="7000" Action="{a}" />'
                for i, a in enumerate(("PowerCycle", "Socket", "ThermalHead", "Escalate"), 1)))
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")  # a rule with four alarms ...
    fill_new_rule(doc, "x0", "Bin8700", "FB8700", "Socket; Escalate")  # ... as the model of a rule with two
    res = check_doc(doc)
    assert res.ok and res.per_rule == {}, res.text


@pytest.mark.skipif(not REAL_DIE_FILE.is_file(), reason="tests/EMR_MCC_Die.xml is not part of this checkout")
def test_every_rule_of_the_real_die_file_takes_a_list_of_actions_and_stays_valid():
    doc = PcsDieFileDoc(REAL_DIE_FILE.read_bytes(), REAL_DIE_FILE.name)
    settings = Settings(DEFAULT_CONFIG_PATH)
    for row in doc.rows("xofy"):
        assert doc.apply_edits([CellEdit("xofy", row["id"], "action", "PowerCycle; Socket; Escalate")]).errors == []
    assert {row["action"] for row in doc.rows("xofy")} == {"PowerCycle; Socket; Escalate"}
    res = rv.run_check(doc.current_text(), REAL_DIE_FILE.name, settings.alarm_map, settings.skip_workflow_check,
                       settings.bin_digit_exceptions, doc.bin_rules())
    assert res.ok, res.text[:2000]  # (the Orders count 1, 2, 3 in every rule, so the checker has nothing to say)
    assert len(doc.export_bytes()) > 0 and doc.export_mode == "patch"  # edited in place, not written again
    while doc.can_undo:
        doc.undo()
    assert doc.export_bytes() == REAL_DIE_FILE.read_bytes()  # (and undo gives every byte of the file back)


# ----------------------------------------------------------------------------------------- the missing bins step
def write_bins(tmp_path: Path, numbers, program: str = "EMRA1X3B13F2633", prefix: str = "bins") -> Path:
    """The CSV the TP Name box saves: PROGRAM_NAME and BIN_NUMBER."""
    path = tmp_path / f"{prefix}_{program}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["PROGRAM_NAME", "BIN_NUMBER"])
        writer.writerows((program, number) for number in numbers)
    return path


def check_with_program(xml: str, csv_path: Path) -> rv.CheckResult:
    """What the page does once a TP has been looked up: the check with the rules and the TP's CSV."""
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    return rv.run_check(doc.current_text(), "r.xml", Settings(DEFAULT_CONFIG_PATH).alarm_map, True, None, doc.bin_rules(), csv_path)


def check_with_units(xml: str, unit_csv: Path, program_csv: Path | None = None) -> rv.CheckResult:
    """The same with the CSV of the bins the TP's units had (and, if given, the TP's own CSV too)."""
    doc = PcsDieFileDoc(xml.encode("utf-8"), "r.xml")
    return rv.run_check(doc.current_text(), "r.xml", Settings(DEFAULT_CONFIG_PATH).alarm_map, True, None, doc.bin_rules(),
                        program_csv, unit_csv)


def test_the_bins_of_the_program_that_no_rule_has_are_listed_and_the_check_still_passes(tmp_path):
    xml = die_file(("Bin9103", "FB9103"), ("XofY_LowYield", "IB9-99"), ("Bin70xx", "IB70,[FB7056]"))
    res = check_with_program(xml, write_bins(tmp_path, [9103, 91030000, 9104, 91040000, 70, 7056, 9]))
    assert res.ok and res.n_errors == 0  # information only: the pcs_die_file can be saved
    assert res.missing == [9, 9104, 91040000]  # 9 is only in the LowYield rule, which does not count
    assert res.n_missing == 3 and res.n_program_bins == 7
    lines = res.missing_text.splitlines()
    assert lines[0] == "TP 'EMRA1X3B13F2633': 3 of 7 bin(s) are in no rule."
    assert lines[-3:] == ["9", "9104", "91040000"]  # one bin number per line, nothing else
    assert res.text.startswith("No errors found")  # the Error tab says nothing about it


def test_all_bins_in_a_rule_says_so(tmp_path):
    res = check_with_program(die_file(("Bin9103", "FB9103")), write_bins(tmp_path, [9103, 91030000]))
    assert res.missing == [] and res.n_missing == 0 and res.n_program_bins == 2
    assert res.missing_text.splitlines()[0] == "TP 'EMRA1X3B13F2633': all 2 bin(s) are in a rule."


def test_values_of_the_csv_that_are_no_bin_number_are_told(tmp_path):
    res = check_with_program(die_file(("Bin9103", "FB9103")), write_bins(tmp_path, [9103, "abc", ""]))
    assert "2 row(s) of the CSV are no bin number (1 to 8 digits) and were left out." in res.missing_text


def test_a_csv_without_bins_says_so(tmp_path):
    empty = check_with_program(die_file(("Bin9103", "FB9103")), write_bins(tmp_path, []))  # a TP with no bins
    assert empty.ok and empty.missing == [] and empty.n_program_bins == 0
    assert empty.missing_text.splitlines()[0] == "The CSV has no bin number to check."
    junk = check_with_program(die_file(("Bin9103", "FB9103")), write_bins(tmp_path, ["abc"]))
    assert junk.missing_text.splitlines()[0] == "TP 'EMRA1X3B13F2633': the CSV has no bin number to check."
    assert "1 row(s) of the CSV are no bin number" in junk.missing_text


def test_without_a_program_the_tab_says_what_to_do():
    res = check_with_duplicates(die_file(("Bin9103", "FB9103")))  # no CSV
    assert res.missing == [] and res.n_program_bins == 0 and res.n_missing == 0
    assert res.missing_text.startswith("Type a TP name in the TP Name box")


def test_a_csv_that_cannot_be_read_is_told_in_the_tab_and_is_no_error(tmp_path):
    res = check_with_program(die_file(("Bin9103", "FB9103")), tmp_path / "gone" / "bins_X.csv")
    assert res.ok and res.n_missing == 0 and res.warnings == []
    assert res.missing_text == "The bins of the TP were not checked: bins_X.csv cannot be read: the file is gone - query the TP again"


def test_a_crash_in_the_missing_bins_step_does_not_stop_the_check(tmp_path, monkeypatch):
    def boom(rules, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(rv.mb, "find_missing", boom)
    res = check_with_program(die_file(("Bin9103", "FB9103")), write_bins(tmp_path, [9103]))
    assert res.ok and res.missing == []  # the checker's part is untouched
    assert res.warnings == ["Missing bins were not checked: RuntimeError: boom"]
    assert "[warn] Missing bins were not checked" in res.text
    assert res.missing_text == "The bins of the TP were not checked: RuntimeError: boom"


# ----------------------------------------------------------------------------------- the unit level step
def test_the_bins_the_units_had_that_no_rule_has_are_listed_in_their_own_tab(tmp_path):
    xml = die_file(("Bin9103", "FB9103"), ("XofY_LowYield", "IB9-99"), ("Bin70xx", "IB70,[FB7056]"))
    res = check_with_units(xml, write_bins(tmp_path, [91030000, 91040000, 70560000, 91040001], prefix="unit_bins"))
    assert res.ok and res.n_errors == 0  # information only, like the Missing Bins tab
    assert res.unit_missing == [91040000, 91040001] and res.n_unit_missing == 2 and res.n_unit_bins == 4
    lines = res.unit_missing_text.splitlines()
    assert lines[0] == "TP 'EMRA1X3B13F2633' (unit level data): 2 of 4 bin(s) are in no rule."
    assert "most detailed bin" in lines[1]
    assert lines[-2:] == ["91040000", "91040001"]
    assert res.missing == [] and res.n_missing == 0 and res.n_program_bins == 0  # the other tab is not touched
    assert res.missing_text.startswith("Type a TP name in the TP Name box")


def test_the_two_tabs_are_checked_against_their_own_csv(tmp_path):
    xml = die_file(("Bin9103", "FB9103"))
    res = check_with_units(xml, write_bins(tmp_path, [91030000, 92000000], prefix="unit_bins"), write_bins(tmp_path, [9103, 9104, 9105]))
    assert res.missing == [9104, 9105] and res.n_missing == 2 and res.n_program_bins == 3
    assert res.unit_missing == [92000000] and res.n_unit_missing == 1 and res.n_unit_bins == 2


def test_all_unit_bins_in_a_rule_says_so_and_no_unit_data_says_what_to_do(tmp_path):
    xml = die_file(("Bin9103", "FB9103"))
    res = check_with_units(xml, write_bins(tmp_path, [91030000, 91039999], prefix="unit_bins"))
    assert res.unit_missing == [] and res.n_unit_bins == 2
    assert res.unit_missing_text.splitlines()[0] == "TP 'EMRA1X3B13F2633' (unit level data): all 2 bin(s) are in a rule."
    empty = check_with_units(xml, write_bins(tmp_path, [], prefix="unit_bins"))  # a TP without units in the window
    assert empty.ok and empty.unit_missing_text.splitlines()[0] == "The CSV has no bin number to check."
    nothing = check_with_duplicates(xml)  # no unit level CSV yet
    assert nothing.unit_missing == [] and nothing.n_unit_bins == 0
    assert nothing.unit_missing_text == "Type a TP name in the TP Name box: the bins its units had that no rule has are listed here."


def test_a_unit_csv_that_cannot_be_read_is_told_in_its_tab_and_is_no_error(tmp_path):
    res = check_with_units(die_file(("Bin9103", "FB9103")), tmp_path / "gone" / "unit_bins_X.csv")
    assert res.ok and res.n_unit_missing == 0 and res.warnings == []
    assert res.unit_missing_text == (
        "The unit level bins of the TP were not checked: unit_bins_X.csv cannot be read: the file is gone - query the TP again"
    )
    assert res.missing_text.startswith("Type a TP name")  # the other tab does not mind


def test_a_crash_in_the_unit_level_step_does_not_stop_the_check(tmp_path, monkeypatch):
    def boom(rules, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(rv.mb, "find_missing", boom)
    res = check_with_units(die_file(("Bin9103", "FB9103")), write_bins(tmp_path, [9103], prefix="unit_bins"))
    assert res.ok and res.unit_missing == []
    assert res.warnings == ["Unit level missing bins were not checked: RuntimeError: boom"]
    assert "[warn] Unit level missing bins were not checked" in res.text
    assert res.unit_missing_text == "The unit level bins of the TP were not checked: RuntimeError: boom"


def test_the_rules_may_come_as_a_generator_and_both_steps_get_them(tmp_path):
    doc = PcsDieFileDoc(die_file(("Big", "IB91"), ("Small", "FB9103")).encode("utf-8"), "r.xml")
    res = rv.run_check(doc.current_text(), "r.xml", Settings(DEFAULT_CONFIG_PATH).alarm_map, True, None,
                       (rule for rule in doc.bin_rules()), write_bins(tmp_path, [9103, 92]))
    assert res.n_warnings == 1 and res.missing == [92]  # the duplicate step and the missing bins step


@pytest.mark.skipif(not REAL_DIE_FILE.is_file(), reason="tests/EMR_MCC_Die.xml is not part of this checkout")
def test_the_real_die_file_against_a_list_of_bins(tmp_path):
    doc = PcsDieFileDoc(REAL_DIE_FILE.read_bytes(), REAL_DIE_FILE.name)
    settings = Settings(DEFAULT_CONFIG_PATH)
    csv_path = write_bins(tmp_path, [9180, 91800000, 9103, 91030000, 70, 7056, 9, 91])
    res = rv.run_check(doc.current_text(), REAL_DIE_FILE.name, settings.alarm_map, settings.skip_workflow_check,
                       settings.bin_digit_exceptions, doc.bin_rules(), csv_path)
    assert res.ok  # (so it can be saved)
    assert res.missing == [9, 91, 9103, 91030000]  # FB9103 has no rule and the LowYield rule (IB9-99) does not count
