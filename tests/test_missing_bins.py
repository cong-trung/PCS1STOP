"""The Missing Bins step: the bins in the program's CSV that no rule has - read with Polars (``scan_csv``)."""
from __future__ import annotations

import csv
import random
import time
from pathlib import Path

import pytest

from pcs_die_file.missing_bins import BIN_COLUMN, PROGRAM_COLUMN, MissingBinsError, find_missing, rule_ranges
from pcs_die_file.model import PcsDieFileDoc
from services.program_bins import CSV_HEADER, ProgramBinsService

REAL_DIE_FILE = Path(__file__).with_name("EMR_MCC_Die.xml")


def write_csv(path: Path, numbers, program: str = "P", header=("PROGRAM_NAME", "BIN_NUMBER")) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows((program, number) for number in numbers)
    return path


def as_rules(*rules: tuple[str, str]) -> list[tuple[str, str, str]]:
    """(RuleName, Bins) pairs as ``PcsDieFileDoc.bin_rules()`` gives them."""
    return [(f"x{i}", name, bins) for i, (name, bins) in enumerate(rules)]


def missing(tmp_path, rules, numbers) -> list[int]:
    return list(find_missing(as_rules(*rules), write_csv(tmp_path / "bins.csv", numbers)).missing)


def covered(*rules: tuple[str, str]) -> list[str]:
    return [str(r) for r in rule_ranges(as_rules(*rules))]


def test_the_columns_are_the_ones_the_program_box_writes():
    assert (PROGRAM_COLUMN, BIN_COLUMN) == CSV_HEADER == ("PROGRAM_NAME", "BIN_NUMBER")


# ------------------------------------------------------------------------------------------ what the rules cover
def test_a_rule_covers_its_bins_without_the_carve_outs_marked_with_brackets():
    assert covered(("A", "IB70,[FB7056]")) == ["70000000-70999999"]  # the [ ] part is left out, not cut out
    assert covered(("A", "FB8090-8099,[DB80905871]"), ("B", "FB8200")) == ["80900000-80999999", "82000000-82009999"]
    assert covered(("A", "[FB7056]")) == []  # nothing but a carve-out covers nothing


def test_lowyield_rules_do_not_count_in_any_case():
    assert covered(("XofY_LowYield", "IB9-99"), ("LOWYIELD_Cover", "FB3500-3503"), ("A", "FB7006")) == ["70060000-70069999"]


def test_touching_rules_join_and_a_token_that_is_no_bin_is_skipped():
    assert covered(("A", "FB9100-9104"), ("B", "FB9105-9109")) == ["91000000-91099999"]
    assert covered(("A", "FB7006,xx,FB7007,")) == ["70060000-70079999"]  # the checker reports xx, the rest still counts
    assert covered(("A", ""), ("B", None)) == []


# ---------------------------------------------------------------------------------------------- the check
def test_a_bin_is_in_a_rule_when_all_of_it_is(tmp_path):
    rules = [("A", "FB9103")]
    assert missing(tmp_path, rules, [9103, 91030000, 91039999, 9104, 91040000, 91]) == [91, 9104, 91040000]
    # an FB is not in a rule that only has one of its DBs; the DB is
    assert missing(tmp_path, [("A", "DB91030000")], [9103, 91030000, 91030001]) == [9103, 91030001]
    # an IB rule has everything below it
    assert missing(tmp_path, [("A", "IB91")], [91, 9103, 91030000, 92, 9200]) == [92, 9200]


def test_every_width_of_bin_number_in_the_real_lists_is_understood(tmp_path):
    """The lists have 1, 2, 3, 4, 7 and 8 digits: IB exceptions, IBs, FB exceptions, FBs, DB exceptions and DBs."""
    rules = [("A", "IB9-99")]  # 90000000-99999999
    assert missing(tmp_path, rules, [9, 91, 9103, 91030000, 900, 9000000, 8, 80, 100, 1000000]) == [100, 1000000, 8, 80]
    # 100 = 10000000-10099999 comes before 1000000 = 10000000-10000009 (the wider one first), then 8 and 80


def test_carve_outs_are_ignored_so_ib70_has_ib70_as_a_whole(tmp_path):
    rules = [("XofY_Bin70xx", "IB70,[FB7056]")]
    assert missing(tmp_path, rules, [70, 7056, 70560000, 7057]) == []
    assert missing(tmp_path, rules, [71, 7156]) == [71, 7156]


def test_the_bins_of_lowyield_rules_are_not_counted(tmp_path):
    rules = [("XofY_LowYield", "IB9-99")]
    assert missing(tmp_path, rules, [9103, 91030000]) == [9103, 91030000]
    assert missing(tmp_path, rules + [("XofY_Bin9103", "FB9103")], [9103, 91030000, 9104]) == [9104]


def test_touching_rules_have_what_none_of_them_has_alone(tmp_path):
    rules = [("A", "FB9100-9104"), ("B", "FB9105-9109")]
    assert missing(tmp_path, rules, [910, 9105, 91050000]) == []  # 910 = 91000000-91099999


def test_the_missing_bins_come_in_order_of_the_bin_with_an_fb_before_its_dbs(tmp_path):
    assert missing(tmp_path, [], [91030001, 9104, 9103, 91030000, 91, 91040000]) == [91, 9103, 91030000, 91030001, 9104, 91040000]


def test_a_bin_is_counted_once_and_values_that_are_no_bin_are_left_out(tmp_path):
    path = write_csv(tmp_path / "bins.csv", [9103, "9103", " 9103 ", "abc", "", "123456789", "-5", "91.5", 91030000])
    found = find_missing(as_rules(("A", "FB9103")), path)
    assert (found.checked, found.ignored, found.missing) == (2, 5, ())  # 9103 and 91030000; 5 rows are no bin number
    assert found.programs == ("P",)


def test_no_rules_means_every_bin_is_missing_and_no_bins_means_nothing_is(tmp_path):
    found = find_missing([], write_csv(tmp_path / "a.csv", [9103, 91030000]))
    assert (found.checked, found.missing) == (2, (9103, 91030000))
    found = find_missing(as_rules(("A", "IB91")), write_csv(tmp_path / "b.csv", []))
    assert (found.checked, found.ignored, found.missing, found.programs) == (0, 0, (), ())


def test_the_program_names_come_from_the_csv(tmp_path):
    path = tmp_path / "bins.csv"
    path.write_text("PROGRAM_NAME,BIN_NUMBER\nQ,1\nP,2\nQ,3\n", encoding="utf-8")
    assert find_missing([], path).programs == ("Q", "P")


def test_the_columns_are_found_by_name_and_the_others_are_not_read(tmp_path):
    path = tmp_path / "bins.csv"
    path.write_text("BIN_DESCRIPTION,BIN_NUMBER,PROGRAM_NAME\nACTUATE,9103,P\nFAILURE,91030000,P\n", encoding="utf-8")
    found = find_missing(as_rules(("A", "FB9103")), path)
    assert (found.checked, found.missing, found.programs) == (2, (), ("P",))


def test_what_the_program_box_writes_is_what_the_check_reads(tmp_path):
    """The CSV of ProgramBinsService (its own writer) read back by the check."""
    lookup = ProgramBinsService(tmp_path / "unused", out_dir=tmp_path)
    path = lookup._save("EMRA1X3B13F2633", (("EMRA1X3B13F2633", 9103), ("EMRA1X3B13F2633", 91030000), ("EMRA1X3B13F2633", 9104)))
    found = find_missing(as_rules(("A", "FB9103")), path)
    assert found.programs == ("EMRA1X3B13F2633",) and found.checked == 3 and found.missing == (9104,)


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (None, "the file is gone - query the program again"),
        ("", "it is empty"),
        ("A,B\n1,2\n", "it needs the columns PROGRAM_NAME and BIN_NUMBER"),
    ],
)
def test_a_csv_that_cannot_be_read_says_so_with_its_name_and_no_folder(tmp_path, content, why):
    path = tmp_path / "somewhere" / "bins_P.csv"
    if content is not None:
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")
    with pytest.raises(MissingBinsError) as caught:
        find_missing(as_rules(("A", "FB9103")), path)
    assert str(caught.value) == f"bins_P.csv cannot be read: {why}"
    assert "somewhere" not in str(caught.value)


def test_it_agrees_with_a_plain_check_of_every_bin_and_is_quick(tmp_path):
    """50,000 random bin numbers of every width against the rules of the real die file - or a handful without it."""
    if REAL_DIE_FILE.is_file():
        rules = PcsDieFileDoc(REAL_DIE_FILE.read_bytes(), REAL_DIE_FILE.name).bin_rules()
    else:
        rules = as_rules(("A", "IB70,[FB7056]"), ("B", "FB8805,[DB88050218]"), ("C", "FB9180"), ("D", "DB79061501"), ("E", "FB8090-8099"))
    cover = rule_ranges(rules)
    rnd = random.Random(7)
    numbers = [rnd.choice([rnd.randint(0, 99), rnd.randint(100, 999), rnd.randint(1000, 9999), rnd.randint(10**7, 10**8 - 1)]) for _ in range(30000)]
    numbers += [int(f"{rnd.choice([70, 79, 80, 87, 88, 91, 98]):02d}{rnd.randint(0, 999999):06d}") for _ in range(20000)]
    path = write_csv(tmp_path / "bins.csv", numbers)

    started = time.perf_counter()
    found = find_missing(rules, path)
    assert time.perf_counter() - started < 5

    def is_in_a_rule(number: int) -> bool:
        low, high = int(str(number).ljust(8, "0")), int(str(number).ljust(8, "9"))
        return any(r.start <= low and high <= r.end for r in cover)

    expected = sorted({n for n in numbers if not is_in_a_rule(n)}, key=lambda n: (int(str(n).ljust(8, "0")), -int(str(n).ljust(8, "9"))))
    assert list(found.missing) == expected and found.checked == len(set(numbers))


@pytest.mark.skipif(not REAL_DIE_FILE.is_file(), reason="tests/EMR_MCC_Die.xml is not part of this checkout")
def test_the_real_die_file_against_a_list_in_the_shape_of_a_program(tmp_path):
    rules = PcsDieFileDoc(REAL_DIE_FILE.read_bytes(), REAL_DIE_FILE.name).bin_rules()
    wanted = [
        9180, 91800000,  # XofY_Bin9180_GeneralHardwareFailure: FB9180
        70, 7006, 70060000,  # XofY_Bin70xx_DragonBinSwitching: IB70,[FB7056] - the carve-out is not cut out
        7056, 70560000,  # ... so FB7056 is in a rule too
        80905871,  # DB80905871 has its own rule (and is a carve-out of XofY_Bin8090_SDE)
        88050218,  # ... and so has DB88050218
    ]
    unwanted = [9103, 91030000, 9, 91, 98, 1]  # FB9103 has no rule; XofY_LowYield (IB9-99) does not count; 9, 91, 98 are IBs
    found = find_missing(rules, write_csv(tmp_path / "bins.csv", wanted + unwanted))
    assert set(found.missing) == {9103, 91030000, 9, 91, 98, 1}
