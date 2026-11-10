"""Duplicate bin ranges between XOFYBIN rules - the rules of dupilcate.md, and the real die file next to this file."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from pcs_die_file.duplicates import bin_range, effective_ranges, find_duplicates, format_ranges, split_bins
from pcs_die_file.model import PcsDieFileDoc

REAL_DIE_FILE = Path(__file__).with_name("EMR_MCC_Die.xml")


def ranges(expression: str) -> list[str]:
    return [str(r) for r in effective_ranges(*split_bins(expression))]


def duplicates(*rules: tuple[str, str] | tuple[str, str, str]) -> dict[str, list[str]]:
    """{rule: [the rules it lies inside of]} for (RuleName, Bins[, ValidOperations]) tuples ("" when not given)."""
    items = [(f"r{i}", rule[0], rule[1], rule[2] if len(rule) > 2 else "") for i, rule in enumerate(rules)]
    found = find_duplicates(items)
    return {item.rule.name: [parent.name for parent in item.parents] for item in found}


# ----------------------------------------------------------------------------------------------- bins -> ranges
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("DB88050218", "88050218-88050218"),
        ("FB7006", "70060000-70069999"),
        ("FB8805", "88050000-88059999"),
        ("IB70", "70000000-70999999"),
        ("IB9", "90000000-99999999"),
        ("FB8090-8099", "80900000-80999999"),
        ("IB9-99", "90000000-99999999"),
        ("DB88050218-88050300", "88050218-88050300"),
        ("fb7006", "70060000-70069999"),  # the checker does not care about case either
        ("  FB7006 ", "70060000-70069999"),
        ("FB100", "10000000-10099999"),  # the digit-count exceptions of the checker pad the same way
        ("IB09", "09000000-09999999"),
        ("FB0123", "01230000-01239999"),
    ],
)
def test_a_bin_becomes_a_range_of_8_digits(token, expected):
    assert str(bin_range(token)) == expected


@pytest.mark.parametrize(
    "token",
    ["", "FB", "XB7006", "FB70a6", "FB7006-", "FB7006-7007-7008", "FB123456789", "FB8099-8090", "[FB7006]", "FB 7006", "70"],
)
def test_what_is_no_bin_has_no_range(token):
    assert bin_range(token) is None


def test_the_bins_of_an_expression_and_its_carve_outs_are_told_apart():
    normal, carved = split_bins("FB8805,[DB88050218], [DB88051002] ,[DB88052829]")
    assert [str(r) for r in normal] == ["88050000-88059999"]
    assert [str(r) for r in carved] == ["88050218-88050218", "88051002-88051002", "88052829-88052829"]


@pytest.mark.parametrize("expression", ["", "FB7006,", ",FB7006", "FB7006,xx", "FB7006,[xx]", "[FB7006", "FB7006]", "FB7006,[[FB7056]]"])
def test_an_expression_with_something_unusable_is_not_read_at_all(expression):
    assert split_bins(expression) is None


# ------------------------------------------------------------------------------------------ effective ranges
def test_a_carve_out_is_cut_out_of_the_bins():
    assert ranges("IB70,[FB7056]") == ["70000000-70559999", "70570000-70999999"]
    assert ranges("FB8090-8099,[DB80905871]") == ["80900000-80905870", "80905872-80999999"]
    assert ranges("FB8805,[DB88050218],[DB88051002],[DB88052829]") == [
        "88050000-88050217",
        "88050219-88051001",
        "88051003-88052828",
        "88052830-88059999",
    ]
    assert ranges("FB8004,FB8006,[DB80060956],[DB80062229]") == [
        "80040000-80049999",
        "80060000-80060955",
        "80060957-80062228",
        "80062230-80069999",
    ]


def test_touching_and_overlapping_ranges_are_joined():
    assert ranges("FB7000,FB7001") == ["70000000-70019999"]  # 70000000-70009999 + 70010000-70019999
    assert ranges("FB7000-7001,FB7002") == ["70000000-70029999"]
    assert ranges("IB70,FB7006") == ["70000000-70999999"]
    assert ranges("FB7002,FB7000") == ["70000000-70009999", "70020000-70029999"]  # a gap stays a gap, order does not matter


def test_a_carve_out_at_the_edge_or_across_several_ranges():
    assert ranges("FB7000-7002,[DB70000000]") == ["70000001-70029999"]
    assert ranges("FB7000-7002,[DB70029999]") == ["70000000-70029998"]
    assert ranges("FB7000,FB7002,[FB7000-7002]") == []  # covers both


def test_a_carve_out_that_is_not_in_the_bins_changes_nothing():
    assert ranges("FB7006,[FB7056]") == ["70060000-70069999"]


def test_nothing_is_left_when_everything_is_carved_out_or_nothing_is_watched():
    assert ranges("FB7006,[FB7006]") == []
    assert ranges("FB7006,[IB70]") == []
    assert effective_ranges([], []) == []


def test_ranges_are_written_like_the_csv_of_dupilcate_md():
    assert format_ranges(effective_ranges(*split_bins("IB70,[FB7056]"))) == "70000000-70559999;70570000-70999999"


# ------------------------------------------------------------------------------------------- the duplicates
def test_a_smaller_rule_inside_a_bigger_one_is_a_duplicate():
    assert duplicates(("Bin70xx", "IB70,[FB7056]"), ("Bin7006", "FB7006")) == {"Bin7006": ["Bin70xx"]}
    assert duplicates(("Bin7006", "FB7006"), ("Bin70xx", "IB70,[FB7056]")) == {"Bin7006": ["Bin70xx"]}  # file order is no matter
    assert duplicates(("Bin8803", "FB8803"), ("Bin88030218", "DB88030218")) == {"Bin88030218": ["Bin8803"]}


def test_rules_that_do_not_meet_are_no_duplicates():
    assert duplicates(("Bin8805", "FB8805"), ("Bin7006", "FB7006")) == {}


def test_a_rule_that_sits_in_a_carve_out_is_no_duplicate():
    """The four patterns of dupilcate.md (EXPLICIT_EXCEPTION): the carve-out is a deliberate hole in the bigger rule."""
    assert duplicates(("Bin70xx", "IB70,[FB7056]"), ("Bin7056", "FB7056")) == {}
    assert duplicates(("Bin8090", "FB8090-8099,[DB80905871]"), ("Bin80905871", "DB80905871")) == {}
    eight805 = ("Bin8805", "FB8805,[DB88050218],[DB88051002],[DB88052829]")
    assert duplicates(eight805, ("a", "DB88050218"), ("b", "DB88051002"), ("c", "DB88052829")) == {}
    eight004 = ("Bin8004", "FB8004,FB8006,[DB80060956],[DB80062229]")
    assert duplicates(eight004, ("a", "DB80060956"), ("b", "DB80062229")) == {}


def test_a_bin_next_to_the_carve_out_is_a_duplicate_again():
    eight805 = ("Bin8805", "FB8805,[DB88050218],[DB88051002],[DB88052829]")
    assert duplicates(eight805, ("hole", "DB88050218"), ("beside", "DB88050219")) == {"beside": ["Bin8805"]}


def test_a_rule_that_is_only_partly_inside_is_left_alone():
    assert duplicates(("A", "FB7000-7055"), ("B", "FB7050-7060")) == {}  # PARTIAL_OVERLAP
    # real data: Dragon watches FB7056 (carved out of Bin70xx) and FB7054, FB7004, FB7006 (inside it)
    assert duplicates(("Bin70xx", "IB70,[FB7056]"), ("Dragon", "FB7056,FB7054,FB7004,FB7006")) == {}
    assert duplicates(("A", "FB7006,FB7156"), ("B", "FB7000-7009")) == {}  # one range inside, one outside


def test_every_range_of_a_rule_has_to_be_inside():
    assert duplicates(("A", "FB7006,FB7156"), ("B", "FB7000-7009,FB7150-7159")) == {"A": ["B"]}
    assert duplicates(("A", "FB7006,FB7156"), ("B", "IB70"), ("C", "IB71")) == {}  # inside both together, inside neither


def test_rules_with_the_same_bins_are_duplicates_of_each_other():
    assert duplicates(("A", "FB7006"), ("B", "FB7006")) == {"A": ["B"], "B": ["A"]}
    assert duplicates(("A", "FB7000,FB7001"), ("B", "FB7000-7001")) == {"A": ["B"], "B": ["A"]}  # same ranges written differently


def test_a_rule_can_be_inside_several_rules_and_they_come_in_file_order():
    found = duplicates(("Big", "IB70"), ("Mid", "FB7000-7009"), ("Small", "FB7006"), ("Elsewhere", "FB8805"))
    assert found == {"Mid": ["Big"], "Small": ["Big", "Mid"]}
    assert duplicates(("Small", "FB7006"), ("Mid", "FB7000-7009"), ("Big", "IB70")) == {"Small": ["Mid", "Big"], "Mid": ["Big"]}


def test_ranges_that_start_together_hold_each_other_correctly():
    assert duplicates(("Small", "FB7000"), ("Big", "IB70")) == {"Small": ["Big"]}  # same start, the bigger one ends later
    assert duplicates(("Big", "IB70"), ("Small", "FB7000")) == {"Small": ["Big"]}


def test_duplicates_are_only_looked_for_inside_the_same_valid_operations():
    assert duplicates(("Bin70xx", "IB70", "VPO"), ("Bin7006", "FB7006", "VPO")) == {"Bin7006": ["Bin70xx"]}
    assert duplicates(("Bin70xx", "IB70", "VPO"), ("Bin7006", "FB7006", "SORT")) == {}  # no direction makes a pair
    assert duplicates(("Bin7006", "FB7006", "SORT"), ("Bin70xx", "IB70", "VPO")) == {}
    assert duplicates(("A", "FB7006", "VPO"), ("B", "FB7006", "SORT")) == {}  # not even the same bins


def test_rules_without_valid_operations_are_a_group_of_their_own():
    assert duplicates(("Bin70xx", "IB70"), ("Bin7006", "FB7006")) == {"Bin7006": ["Bin70xx"]}  # both ""
    assert duplicates(("Bin70xx", "IB70"), ("Bin7006", "FB7006", "VPO")) == {}  # "" is no wildcard
    assert duplicates(("Bin70xx", "IB70", " VPO "), ("Bin7006", "FB7006", "VPO")) == {"Bin7006": ["Bin70xx"]}  # trimmed


def test_every_operations_value_is_its_own_group_and_file_order_is_kept():
    assert duplicates(("BigV", "IB70", "VPO"), ("BigS", "IB70", "SORT"), ("SmallV", "FB7006", "VPO"),
                      ("SmallS", "FB7006", "SORT")) == {"SmallV": ["BigV"], "SmallS": ["BigS"]}


@pytest.mark.parametrize("name", ["XofY_LowYield", "XofY_LowYield_Cover", "LowYield_Bin9", "lowyield", "XOFY_LOWYIELD", "a_LoWyIeLd_b"])
def test_lowyield_rules_are_not_looked_at_in_either_direction(name):
    assert duplicates((name, "IB9-99"), ("Bin9850", "FB9850")) == {}  # as the bigger rule
    assert duplicates(("Bin70xx", "IB70"), (name, "FB7006")) == {}  # as the smaller rule
    assert duplicates((name, "FB7006"), (name + "2", "FB7006")) == {}  # and between each other


def test_rules_that_cannot_be_read_are_skipped():
    assert duplicates(("Big", "IB70"), ("Bad", "FB70xx"), ("Empty", ""), ("Comma", "FB7006,"), ("Backwards", "FB7009-7000")) == {}
    assert duplicates(("Big", "IB70"), ("Nothing", "[FB7006]"), ("Cancelled", "FB7006,[FB7006]")) == {}  # covers nothing


def test_a_rule_without_a_name_is_compared_too():
    found = find_duplicates([("x0", "Big", "IB70", "VPO"), ("x1", "", "FB7006", "VPO"), ("x2", None, "FB7007", "VPO")])
    assert [(item.rule.key, item.rule.name) for item in found] == [("x1", ""), ("x2", "")]
    assert [parent.key for parent in found[0].parents] == ["x0"]


def test_a_duplicate_knows_its_bins_ranges_and_operations_and_those_of_its_parent():
    (found,) = find_duplicates([("a", "Bin70xx", " IB70,[FB7056] ", " VPO "), ("b", "Bin7006", "FB7006", "VPO")])
    assert (found.rule.key, found.rule.name, found.rule.bins, found.rule.valid_operations) == ("b", "Bin7006", "FB7006", "VPO")
    assert format_ranges(found.rule.ranges) == "70060000-70069999"
    (parent,) = found.parents
    assert (parent.key, parent.name, parent.bins, parent.valid_operations) == ("a", "Bin70xx", "IB70,[FB7056]", "VPO")
    assert format_ranges(parent.ranges) == "70000000-70559999;70570000-70999999"


def test_nothing_in_nothing_out():
    assert find_duplicates([]) == []
    assert find_duplicates([("a", "Only", "FB7006", "")]) == []


def test_a_big_file_is_still_quick():
    """Every FB inside its IB and every DB inside its FB: 20,200 rules, 20,100 duplicates - and no all-against-all."""
    rules = [(f"i{i}", f"IB{i:02d}", f"IB{i:02d}", "") for i in range(100)]
    rules += [(f"f{i}", f"FB{i:04d}", f"FB{i:04d}", "") for i in range(10000)]
    rules += [(f"d{i}", f"DB{i:04d}0001", f"DB{i:04d}0001", "") for i in range(10000)]
    started = time.perf_counter()
    found = find_duplicates(rules)
    assert time.perf_counter() - started < 5
    assert len(found) == 20000
    by_key = {item.rule.key: [parent.key for parent in item.parents] for item in found}
    assert by_key["f1234"] == ["i12"] and by_key["d1234"] == ["i12", "f1234"] and "i12" not in by_key


# ----------------------------------------------------------------------------------------- the real die file
needs_real_file = pytest.mark.skipif(not REAL_DIE_FILE.is_file(), reason="tests/EMR_MCC_Die.xml is not part of this checkout")


def real_duplicates() -> dict[str, list[str]]:
    doc = PcsDieFileDoc(REAL_DIE_FILE.read_bytes(), REAL_DIE_FILE.name)
    return {item.rule.name: [parent.name for parent in item.parents] for item in find_duplicates(doc.bin_rules())}


@needs_real_file
def test_the_real_die_file_has_duplicates():
    found = real_duplicates()
    assert found["XofY_Bin88030218_EFIBoot"] == ["XofY_Bin8803_BootHalt"]  # DB88030218 inside FB8803
    assert found["XofY_Bin7900_DUTQDF"] == ["XofY_Bin7900_Generic"]
    for name in ("XofY_Bin7906_DMI", "XofY_Bin7906_RPB_Detect", "XofY_Bin7906_RPB_Swap", "XofY_Bin7906_Margin_UPI"):
        assert found[name] == ["XofY_Bin7906_PYTHON_Fail"]


@needs_real_file
def test_the_real_die_file_carve_outs_and_lowyield_rules_are_no_duplicates():
    found = real_duplicates()
    for name in (
        "XofY_Bin80905871",  # DB80905871 is carved out of XofY_Bin8090_SDE
        "XofY_Bin88050218_EFIBoot",  # DB88050218, DB88051002, DB88052829 are carved out of XofY_Bin8805_EFIBoot
        "XofY_Bin7056_DragonBin",  # FB7056 is carved out of XofY_Bin70xx_DragonBinSwitching
        "XofY_Bin8713_EFIBootRepeat",  # only some of its DBs are inside FB8713
        "XofY_LowYield",
        "XofY_LowYield_Cover",
    ):
        assert name not in found, name
    assert not any(parent.endswith(("LowYield", "LowYield_Cover")) for parents in found.values() for parent in parents)


# ------------------------------------------------------ the NVLH die file (two Valid Operations groups, and the "" rules)
NVLH_DIE_FILE = Path(__file__).with_name("SST_Die_NVLH.xml")
needs_nvlh_file = pytest.mark.skipif(
    not NVLH_DIE_FILE.is_file(), reason="tests/SST_Die_NVLH.xml is not part of this checkout"
)


def nvlh_rules() -> list[tuple[str, str, str, str]]:
    return PcsDieFileDoc(NVLH_DIE_FILE.read_bytes(), NVLH_DIE_FILE.name).bin_rules()


def nvlh_duplicates() -> dict[str, list[str]]:
    return {item.rule.name: [parent.name for parent in item.parents] for item in find_duplicates(nvlh_rules())}


def nvlh_operations() -> dict[str, str]:
    return {name: ops for _key, name, _bins, ops in nvlh_rules()}


@needs_nvlh_file
def test_the_nvlh_die_file_still_finds_the_duplicates_of_one_valid_operations_group():
    found, ops = nvlh_duplicates(), nvlh_operations()
    assert ops["75530030"] == ops["7553"] == "7297,7899,7295"
    assert found["75530030"] == ["7553"]  # DB75530030 inside FB7553, both of the 7297,... group
    assert found["96130010"] == ["9613"]
    assert ops["71020010"] == ops["7102"] == ""  # the rules outside any group ("") hold together too
    assert found["71020010"] == ["7102"]


@needs_nvlh_file
def test_the_nvlh_die_file_compares_no_rules_of_different_valid_operations():
    """The RV_* rules watch the same bins as rules of the 7297,... group; the other Valid Operations keeps them apart."""
    found, ops = nvlh_duplicates(), nvlh_operations()
    for name in ("RV_9850", "RV_90000105", "RV_73100000", "RV_96190000", "RV_90000111"):
        assert ops[name] == "2395,2397,6304"
        assert name not in found, name
    assert ops["9850"] == ops["90000105"] == "7297,7899,7295"  # the same bins, watched by the other group
    for name in ("9850", "90000105", "73100000", "96190000", "90000111"):
        assert name not in found, name  # and inside their own group they lie inside nothing either
    assert not any(name.startswith("RV_") for parents in found.values() for name in parents)


@needs_nvlh_file
def test_the_nvlh_die_file_db_inside_an_fb_of_another_group_is_no_duplicate():
    found, ops = nvlh_duplicates(), nvlh_operations()
    assert ops["71029999"] == "7297,7899,7295" and ops["7102"] == ""
    assert "71029999" not in found  # DB71029999 lies inside FB7102, but the groups differ
    assert ops["91000105"] == "7297,7899,7295" and ops["9100"] == ""
    assert "91000105" not in found  # DB91000105 lies inside FB9100 ("") - the same story
