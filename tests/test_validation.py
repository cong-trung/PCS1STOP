from __future__ import annotations

import json
import re
from pathlib import Path

from config import DEFAULT_CONFIG_PATH, Settings
from pcs_die_file import validation as rv

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