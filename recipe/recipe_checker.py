#!/usr/bin/env python3
"""Python port of the iLLIAD Recipe Checker (iLLIADRecipeParser.CheckRecipeString).

Usage: python recipe_checker.py EMR_MCC_Die.xml [--alarm-map AlarmIdMapping.xml] [--skip-workflow-check]

Ported from SkyLine.Dpc.iLLIAD.dll v3.0.0.83 / SkyLine.Common.Dpc.dll (SkyLine.Dpc.iLLIAD.SkyHookUI.exe).
Supported rules: XofYBin, XofYParametric, LotLimit, Parametric. Other rule names are reported
as unsupported by this port.
"""
import argparse
import copy
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

DEFAULT_INT = -999999999
DEFAULT_ALARM_ID = "99999999999"
MIN_BIN_OR_TOKEN, MAX_BIN_OR_TOKEN = 0, 100000000

ENUMS = {
    "SkipRuleMode": ["SkipOnMatchOrMissing", "SkipOnMatch", "ExecuteOnMatchOrMissing", "ExecuteOnMatch"],
    "CollateralType": ["UNSPECIFIED", "TIU", "TIU_Socket", "Tester", "Tester_Socket", "ActuationPan",
                       "ThermalPan", "ThermalHead", "Contactor", "TCC", "Site", "Cell", "Cell_Socket",
                       "PNB", "PNBHEAD", "PNBSHUTTLE", "PNBLANE", "PNBGANTRY", "TTS", "TTSLANE", "IOLP",
                       "LPSTACKER", "XIU", "Binner", "OLP", "ILP", "OLPSTACKER", "ILPSTACKER", "CHILLER",
                       "Module"],
    "AlarmSeverity": ["PersonalSafety_1", "EquipmentSafety_2", "ParameterControlWarning_3",
                      "ParameterControlError_4", "IrrecoverableError_5", "EquipmentStatusWarning_6",
                      "AttentionFlags_7", "DataIntegrity_8"],
    "SoftResetType": ["Disable", "TriggeredTypeOnly", "All"],
    "TriggerType": ["Unscheduled", "Scheduled"],
    "ParametricStat": ["UNSPECIFIED", "MEAN", "MIN", "MEDIAN", "MAX", "QUANTILE", "STDDEV"],
    "TriggerIncrementMethod": ["Count", "Max"],
}
PCS_COLLATERAL = ["TIU", "TIU_Socket", "Tester", "Tester_Socket", "ActuationPan", "ThermalPan",
                  "ThermalHead", "Contactor", "TCC", "Site", "Cell", "Cell_Socket"]
SPECIAL_TOKENS = ["RepeatOrder", "RuleName", "VID", "IBin", "FBin", "DBin", "Cell", "Socket", "Lot",
                  "ProcessStep", "PJID", "RetestNumber"]


# ---------------------------------------------------------------- error tracker
class RecipeErrorTracker:
    """Port of DpcUtility.iLLIAD.RecipeErrorTracker."""

    def __init__(self):
        self.accumulated = []
        self.parent = None
        self.child = None
        self.group = ""
        self.latest = []
        self.names = set()

    def move_to_node(self, parent, child=None, group=""):
        if self.latest:
            if self.parent is not None:
                text = ("   " + xlinq(self.parent)).replace("\n", "\n   ")
                if self.child is not None:
                    value = xlinq(self.child).split("\n")[0]
                    idx = text.find(value)
                    if idx >= 0:
                        chars = list(text)
                        i = idx - 1
                        while i >= 0 and chars[i] == " ":
                            chars[i] = ">"
                            i -= 1
                        text = "".join(chars)
                else:
                    lines = text.split("\n")
                    hit = False
                    for n, line in enumerate(lines):
                        mark = False
                        for err in self.latest:
                            if "Found extra attribute:" in err or "Found duplicate RuleName values." in err:
                                a, b = err.find("[") + 1, err.find("]")
                                if a > 0 and b > a and err[a:b] in line:
                                    mark = True
                        if mark:
                            hit = True
                            chars = list(line)
                            j = 0
                            while j < len(chars) and chars[j] == " ":
                                chars[j] = ">"
                                j += 1
                            lines[n] = "".join(chars)
                    if hit:
                        text = "\n".join(lines)
                g = "" if self.group == "" else "GroupName = " + self.group
                self.accumulated.append(
                    f" Recipe Node: {g}\n{text}\n Errors:\n     " + "\n     ".join(self.latest) + "\n")
            else:
                self.accumulated.append(" General Errors:\n     " + "\n     ".join(self.latest) + "\n")
            self.latest = []
        self.group, self.child, self.parent = group, child, parent

    def get_accumulated_error_string(self):
        self.move_to_node(None)
        if not self.accumulated:
            return ""
        width = max(len(l) for b in self.accumulated for l in b.split("\n"))
        bar = "*" * width + "\n"
        return bar + bar.join(self.accumulated) + bar

    def add_general_error(self, msg):
        self.latest.append(f"Error: {msg}")

    def add_invalid_attribute(self, name, value, msg):
        self.latest.append(f'Invalid Attribute: [{name}="{value}"] Error: {msg}')

    def add_missing_attribute(self, name, type_name, parent):
        self.latest.append(f"Missing RequiredAttribute: [{name}] of type [{type_name}] in definition of {parent}")

    def add_extra_attribute(self, name, value):
        self.latest.append(f'Found extra attribute: [{name}="{value}"]')

    def check_unique_name(self, rule_name):
        if rule_name in self.names:
            self.latest.append(
                f'Found duplicate RuleName values.  RuleName must be unique within a recipe. '
                f'Name: [RuleName="{rule_name}"]')
            return False
        self.names.add(rule_name)
        return True


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xlinq(e, depth=0):
    """Mimics XElement.ToString() (indented, 2 spaces, ' />' for empty elements)."""
    pad = "  " * depth
    attrs = "".join(f' {_local(k)}="{_esc(v).replace(chr(34), "&quot;")}"' for k, v in e.attrib.items())
    tag = _local(e.tag)
    kids = [c for c in e if isinstance(c.tag, str)]
    text = (e.text or "").strip()
    if not kids and not text:
        return f"{pad}<{tag}{attrs} />" if depth else f"<{tag}{attrs} />"
    if not kids:
        s = f"<{tag}{attrs}>{_esc(e.text)}</{tag}>"
        return pad + s if depth else s
    inner = "\n".join(xlinq(c, depth + 1) for c in kids)
    head = f"<{tag}{attrs}>"
    return f"{pad}{head}\n{inner}\n{pad}</{tag}>" if depth else f"{head}\n{inner}\n</{tag}>"


# ---------------------------------------------------------------- attribute helper
class Attr:
    """Port of DpcUtility.iLLIAD.AttributeHelper. Consumes keys from the dict."""

    @staticmethod
    def _raw(name, attrs, errors, required, parent, type_name):
        keys = [k for k in attrs if k.lower() == name.lower()]
        if len(keys) > 1:
            errors.add_general_error(f"Found two matching keys for Attribute: {name}")
            return None
        if not keys:
            if required:
                errors.add_missing_attribute(name, type_name, parent)
            return None
        val = attrs.pop(keys[0])
        if val is None or val == "":
            errors.add_invalid_attribute(name, val, "Empty string is not allowed")
            return None
        return val

    @staticmethod
    def string(name, attrs, errors, required=False, parent=""):
        v = Attr._raw(name, attrs, errors, required, parent, "string")
        return v if v is not None else ""

    @staticmethod
    def boolean(name, attrs, errors, required=False, parent=""):
        v = Attr._raw(name, attrs, errors, required, parent, "bool")
        if v is None:
            return False
        if v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        errors.add_invalid_attribute(name, v, "Failed to parse to a bool")
        return False

    @staticmethod
    def integer(name, attrs, errors, required=False, parent=""):
        v = Attr._raw(name, attrs, errors, required, parent, "int")
        if v is None:
            return DEFAULT_INT
        try:
            return int(v.strip())
        except ValueError:
            errors.add_invalid_attribute(name, v, "Failed to parse to an int")
            return DEFAULT_INT

    @staticmethod
    def double(name, attrs, errors, required=False, parent=""):
        v = Attr._raw(name, attrs, errors, required, parent, "double")
        if v is None:
            return math.nan
        try:
            return float(v.strip())
        except ValueError:
            errors.add_invalid_attribute(name, v, "Failed to parse to a double")
            return math.nan

    @staticmethod
    def enum(enum_name, name, attrs, errors, required=False, parent=""):
        names = ENUMS[enum_name]
        v = Attr._raw(name, attrs, errors, required, parent, enum_name)
        if v is None:
            return names[0] if enum_name in ("CollateralType", "ParametricStat") else names[0]
        for n in names:
            if n.lower() == v.strip().lower() and n != "UNSPECIFIED":
                return n
        avail = ", ".join(n for n in names if n != "UNSPECIFIED")
        errors.add_invalid_attribute(name, v, f"Failed to parse to an Enum of type {enum_name}. "
                                              f"Available Values: {avail.replace(', ', ',')}")
        return names[0]

    @staticmethod
    def parametric_spec(name, attrs, errors, required=False, parent=""):
        keys = [k for k in attrs if k.lower() == name.lower()]
        if len(keys) > 1:
            errors.add_general_error(f"Found two matching keys for Attribute: {name}")
            return None
        if not keys:
            if required:
                errors.add_missing_attribute(name, "String", parent)
            return None
        text = attrs.pop(keys[0])
        err = ""
        if not text:
            err = "Empty parameter names are not valid"
        else:
            rule_result = re.search(r"{{(.+)}}", text)
            transform = re.search(r"(.+)\[\[(.+)\]\]", text)
            if transform:
                if rule_result:
                    err = f"Can not target RuleResult and use ValueRegex at the same time: '{text}'"
                else:
                    try:
                        re.compile(transform.group(2))
                    except re.error as e:
                        err = f"Exception thrown during Regex Parsing: '{transform.group(2)}' => {e}"
        if err:
            errors.add_invalid_attribute(name, text, err)
        return text

    @staticmethod
    def bin_group(name, attrs, errors, required=False, parent=""):
        v = Attr._raw(name, attrs, errors, required, parent, "BinGroup")
        if v is None:
            return None
        bg = BinGroup(v)
        if not bg.valid:
            errors.add_invalid_attribute(name, v, "Failed to parse to a BinGroup")
        return bg


class BinGroup:
    """Port of DpcUtility.iLLIAD.BinGroup / BinRange. Example: FB9850, DB88050218, FB900-999."""

    def __init__(self, text):
        self.ranges = [self._parse(p.strip()) for p in text.split(",")]
        self.valid = bool(self.ranges) and all(r[3] for r in self.ranges)

    @staticmethod
    def _parse(s):
        # SkyLine BinRange: "[FB8006]" marks an excluded range
        if len(s) > 2 and s.startswith("[") and s.endswith("]"):
            s = s.strip("[] ")
        if len(s) <= 2 or s[:2].upper() not in ("IB", "FB", "DB"):
            return (None, 0, 0, False)
        parts = s[2:].split("-")
        if len(parts) > 2:
            return (s[:2].upper(), 0, 0, False)
        try:
            lo = int(parts[0])
            hi = int(parts[1]) if len(parts) == 2 else lo
        except ValueError:
            return (s[:2].upper(), 0, 0, False)
        return (s[:2].upper(), lo, hi, lo <= hi)


# ---------------------------------------------------------------- alarm id map
class AlarmIdMap:
    def __init__(self, rules=(), bins=(), actions=()):
        self.parts = {"Rule": dict(rules), "BinOrToken": dict(bins), "Action": dict(actions)}

    @classmethod
    def load(cls, path):
        if not path or not os.path.exists(path):
            return None
        root = ET.parse(path).getroot()
        g = lambda tag, item: [(e.get("name"), e.get("id")) for e in root.findall(f"{tag}/{item}")]
        m = cls(g("Rules", "Rule"), g("BinOrTokens", "BinOrToken"), g("Actions", "Action"))
        br = (root.findtext("BinRange") or "").strip()
        m.bin_ranges = _parse_bin_ranges(br)
        return m

    bin_ranges = ()

    def get(self, part, key):
        v = self.parts[part].get(key)
        if v is None and part == "BinOrToken":
            mo = re.fullmatch(r"Bin(\d+)", key)
            if mo:
                n = int(mo.group(1))
                w = 2 if len(str(n)) <= 2 else len(str(n))
                if key == f"Bin{n:0{w}d}" and any(lo <= n <= hi for lo, hi in self.bin_ranges):
                    return f"{n:05d}"
        return v

    def names(self, part):
        return list(self.parts[part])

    def options(self, part):
        return "Options: " + ", ".join(self.parts[part])


def _parse_bin_ranges(s):
    """Returns (lo, hi) pairs; the original expands every int (very slow for huge ranges)."""
    out = []
    for tok in filter(None, (t.strip() for t in s.split(","))):
        if "-" in tok:
            a, b = tok.split("-")
            out.append((int(a), int(b)))
        else:
            out.append((int(tok), int(tok)))
    return out


# ---------------------------------------------------------------- actions
class Alarm:
    NODE = "Alarm"

    def __init__(self, alarm_map, overflow_tokens, skip_workflow_check):
        self.map, self.tokens, self.skip_wf = alarm_map, overflow_tokens, skip_workflow_check
        self.message = self.alarm_id = ""

    def initialize_and_validate(self, attrs, errors):
        rule = Attr.string("Rule", attrs, errors, False, "iLLIADAlarm")
        action = Attr.string("Action", attrs, errors, False, "iLLIADAlarm")
        bot = Attr.string("BinOrToken", attrs, errors, False, "iLLIADAlarm")
        self.message = self.alarm_id = ""
        if rule and action and bot:
            self.message, self.alarm_id = self._validate_raab(rule, action, bot, errors)
        if not self.message:
            self.alarm_id = DEFAULT_ALARM_ID
            self.message = Attr.string("Message", attrs, errors, False, "iLLIADAlarm")
            if not self.message:
                errors.add_general_error(
                    "Either [Rule, BinOrToken, and Action] attributes OR the Message attribute needs to be "
                    "defined for RaiseAlarm actions.")
        params = Attr.string("MessageParameters", attrs, errors, False, "iLLIADAlarm")
        if params:
            parts = params.split("|")
            for p in parts:
                self._validate_special_token(p, errors)
            try:
                _net_format(self.message, ["dummyValue"] * len(parts))
            except (IndexError, ValueError):
                errors.add_general_error("Invalid Message formatting. Message and MessageParameters must match.")
        Attr.enum("AlarmSeverity", "Severity", attrs, errors, True, "iLLIADAlarm")
        wf = Attr.string("Workflow", attrs, errors, False, "iLLIADAlarm")
        if wf:
            self._validate_workflow(wf, errors)

    def _validate_raab(self, rule, action, bot, errors):
        """SkyLine variant: Rule/Action must be in the map; BinOrToken is free-form (no whitespace)."""
        m = self.map
        if m is None:
            return "", ""
        rid, aid = m.get("Rule", rule), m.get("Action", action)
        if rid is None:
            errors.add_general_error(f"{rule} attribute was not in the expected list of Rule tokens. "
                                     + m.options("Rule"))
        if aid is None:
            errors.add_general_error(f"{action} attribute was not in the expected list of Action tokens. "
                                     + m.options("Action"))
        if any(ch.isspace() for ch in bot):
            errors.add_general_error("Whitespace character is not allowed in attribute BinOrToken.")
        return f"{rule} {bot} {action}", (rid or "") + (aid or "")

    @staticmethod
    def _validate_special_token(tok, errors):
        if tok.startswith("Ituff="):
            if not tok[6:]:
                errors.add_general_error("SpecialToken 'Ituff=<tokenName>' must have a non empty <tokenName>.")
        elif tok.startswith("CollateralId="):
            if tok[13:].lower() not in (n.lower() for n in ENUMS["CollateralType"]):
                errors.add_general_error("SpecialToken 'CollateralId=<collateralType>' must have a valid "
                                         "collateral type: " + ", ".join(ENUMS["CollateralType"]))
        elif tok.lower() not in (n.lower() for n in SPECIAL_TOKENS):
            errors.add_general_error("Only the following ituff SpecialTokens are allowed: "
                                     "'Ituff=<tokenName>', 'CollateralId=<collateralType>', '"
                                     + "', '".join(SPECIAL_TOKENS) + "'")

    def _validate_workflow(self, text, errors):
        path, inputs = text, []
        try:
            root = ET.fromstring(text)
            if root.tag == "Workflow":
                path, inputs = root.get("Path"), list(root)
        except ET.ParseError:
            pass
        if not path:
            errors.add_general_error("Workflow node needs to specify a Path attribute.")
        elif not self.skip_wf and not os.path.isfile(path):
            errors.add_general_error(f"Target Workflow does not exist! Path: {path}")
        seen = set()
        for i in inputs:
            key = i.get("Key")
            bad = False
            if not key:
                bad = True
                errors.add_general_error("Found CustomInput without a Key attribute.")
            if i.tag == "CustomInput":
                if not i.get("Value"):
                    bad = True
                    errors.add_general_error("Found CustomInput without a Value attribute")
                if not bad:
                    if key in seen:
                        errors.add_general_error(f"Found duplicated CustomInput Keys: {key}")
                    seen.add(key)
            elif i.tag == "SpecialInput":
                self._validate_special_token(i.get("SpecialToken") or "", errors)


def _net_format(fmt, args):
    """Minimal string.Format: raises IndexError/ValueError on bad placeholders."""
    def sub(m):
        return str(args[int(m.group(1))])
    return re.sub(r"\{(\d+)(?:[^}]*)\}", sub, fmt)


class RepeatTrigger:
    NODE = "RepeatTriggerGroup"

    def __init__(self):
        self.by_order = {}

    def add_child(self, action, attrs, errors):
        order = Attr.integer("Order", attrs, errors, True, "RepeatTrigger")
        if order > 0:
            if order not in self.by_order:
                self.by_order[order] = action
            else:
                errors.add_general_error("Order attributes must not repeat inside a RepeatTrigger group")
        else:
            errors.add_general_error(f"Order must be > 0 but was {order}")

    def initialize_and_validate(self, attrs, errors):
        Attr.integer("RepeatTriggerHistoryLength", attrs, errors, True, "RepeatTrigger")
        Attr.enum("TriggerIncrementMethod", "TriggerIncrementMethod", attrs, errors, False, "RepeatTrigger")
        if len(self.by_order) < 1:
            errors.add_general_error("RepeatTrigger must have at least one Action inside")
        for i in range(1, len(self.by_order) + 1):
            if i not in self.by_order:
                errors.add_general_error(
                    "RepeatTrigger sub actions' orders must start at 1 and increase without missing any numbers.")
                return
        Attr.boolean("OnlyCountSameDataGroup", attrs, errors, False, "RepeatTrigger")
        Attr.boolean("OnlyCountSameProcessStep", attrs, errors, False, "RepeatTrigger")
        Attr.boolean("OnlyCountSameLot", attrs, errors, False, "RepeatTrigger")

    def children(self):
        return list(self.by_order.values())


# ---------------------------------------------------------------- rules
class Limit:
    """iLLIADLimit.InitializeAndValidate + iLLIADLimitBinGroup."""
    BIN_GROUP = False
    RULE_REFERENCE_PROPS = ()

    def __init__(self):
        self.action = None
        self.rule_name = ""
        self.clear_scope = ""
        self.props = {}

    def initialize_and_validate(self, attrs, errors):
        if self.BIN_GROUP:
            self.exclude_bins = Attr.bin_group("ExcludeBins", attrs, errors, False, "iLLIADLimit")
            self.include_bins = Attr.bin_group("IncludeBins", attrs, errors, False, "iLLIADLimit")
        p = "iLLIADLimit"
        ct = Attr.enum("CollateralType", "CollateralType", attrs, errors, True, p)
        if ct not in PCS_COLLATERAL:
            errors.add_general_error(f"CollateralType == {ct} is not supported.  Please choose from: "
                                     + ", ".join(PCS_COLLATERAL))
        Attr.integer("MinN", attrs, errors, False, p)
        Attr.boolean("MinNResetNewLot", attrs, errors, False, p)
        self.rule_name = Attr.string("RuleName", attrs, errors, True, p)
        self.clear_scope = Attr.string("ClearScope", attrs, errors, False, p)
        errors.check_unique_name(self.rule_name)
        retest = Attr.boolean("UseRetestUnitData", attrs, errors, False, p)
        retest_changed = Attr.boolean("UseRetestUnitDataOnlyIfChangedBin", attrs, errors, False, p)
        if retest and retest_changed:
            errors.add_general_error("Can not set both UseRetestUnitData and UseRetestUnitDataOnlyIfChangedBin to True")
        for b in ("UseOnlySameDataGroup", "UseOnlySameProcessStep", "UseOnlySameLot",
                  "ExcludeRetestUnitsIfSameBin"):
            Attr.boolean(b, attrs, errors, False, p)
        skip_name = Attr.string("SkipRuleTokenName", attrs, errors, False, p)
        skip_val = Attr.string("SkipRuleTokenValue", attrs, errors, False, p)
        Attr.enum("SkipRuleMode", "SkipRuleMode", attrs, errors, False, p)
        if skip_val:
            _check_regex("SkipRuleTokenValue", skip_val, errors)
            if not skip_name:
                errors.add_general_error("Must set SkipRuleTokenName when setting SkipRuleTokenValue")
        regex_pairs = {}
        for n in ("ValidSSPEC", "ExcludeSSPEC", "ValidTestMode", "ExcludeTestMode"):
            regex_pairs[n] = Attr.string(n, attrs, errors, False, p)
            if regex_pairs[n]:
                _check_regex(n, regex_pairs[n], errors)
            if n.startswith("Exclude") and regex_pairs["Valid" + n[7:]] and regex_pairs[n]:
                errors.add_general_error(f"Must not define both Valid{n[7:]} and Exclude{n[7:]} values")
        for b in ("SaveRuleOutput", "AutoClear"):
            Attr.boolean(b, attrs, errors, False, p)
        Attr.enum("SoftResetType", "SoftReset", attrs, errors, False, p)
        Attr.enum("TriggerType", "TriggerType", attrs, errors, False, p)
        for d in ("Catalyst_USL", "Catalyst_LSL", "Catalyst_Target", "Catalyst_Center"):
            Attr.double(d, attrs, errors, False, "CatalystFilePassthrough")
        pf = Attr.parametric_spec("ParametricFilterTokenName", attrs, errors, False, p)
        if pf:
            lo = Attr.double("ParametricFilterLowerLimit", attrs, errors, False, p)
            hi = Attr.double("ParametricFilterUpperLimit", attrs, errors, False, p)
            if math.isnan(lo) and math.isnan(hi):
                errors.add_general_error("Need to specify ParametricFilterLowerLimit or ParametricFilterUpperLimit "
                                         "if you specify ParametricFilterTokenName.")
            if _ge(lo, hi):
                errors.add_general_error("ParametricFilterLowerLimit must be less than ParametricFilterUpperLimit")
        Attr.bin_group("IgnoreBins", attrs, errors, False, p)
        self.rule_specific(attrs, errors)

    def rule_specific(self, attrs, errors):
        raise NotImplementedError


def _check_regex(name, value, errors):
    try:
        re.compile(value)
    except re.error as e:
        errors.add_general_error(f"{name} is not a valid regex: {e}")


def _ge(a, b):
    return (not math.isnan(a) and not math.isnan(b)) and a >= b


def _le(a, b):
    return (not math.isnan(a) and not math.isnan(b)) and a <= b


class XofY(Limit):
    BIN_GROUP = True

    def __init__(self, kind):
        super().__init__()
        self.kind = kind  # "XofYBin" | "XofYParametric"

    def rule_specific(self, attrs, errors):
        k = self.kind
        rat = Attr.boolean("ResetAfterTrigger", attrs, errors, False, "XofYLimit")
        rat_re = Attr.string("ResetAfterTriggerRegex", attrs, errors, False, "XofYLimit")
        if rat_re and not rat:
            errors.add_general_error("ResetAfterTriggerRegex can only be used when ResetAfterTrigger is set to true")
        Attr.integer("X", attrs, errors, True, k)
        Attr.integer("Y", attrs, errors, True, k)
        pname = Attr.parametric_spec("ParameterName", attrs, errors, k == "XofYParametric", k)
        if not pname and k == "XofYParametric":
            errors.add_general_error("XofYParametric rule needs to have a valid ParameterName")
        kr = Attr.string("ParameterKeyRegex", attrs, errors, False, k)
        vr = Attr.string("ParameterValueRegex", attrs, errors, False, k)
        for label, val in (("ParameterKeyRegex", kr), ("ParameterValueRegex", vr)):
            if val:
                try:
                    re.compile(val)
                except re.error as e:
                    errors.add_general_error(f"{label} is not a valid regex: {e}")
        if kr and not pname:
            errors.add_general_error("Must define ParameterName if you specify ParameterKeyRegex")
        if vr and not pname:
            errors.add_general_error("Must define ParameterName if you specify ParameterValueRegex")
        inv = Attr.boolean("InvertRegexConditions", attrs, errors, False, k)
        if inv and not pname:
            errors.add_general_error("Must define ParameterName if you specify InvertRegexConditions")
        if k == "XofYBin":
            Attr.bin_group("Bins", attrs, errors, True, k)
        else:
            _parametric_limits(attrs, errors, k)


def _parametric_limits(attrs, errors, parent):
    lf = Attr.double("LowerFilter", attrs, errors, False, parent)
    uf = Attr.double("UpperFilter", attrs, errors, False, parent)
    ll = Attr.double("LowerLimit", attrs, errors, False, parent)
    ul = Attr.double("UpperLimit", attrs, errors, False, parent)
    if math.isnan(ll) and math.isnan(ul):
        errors.add_general_error("Must specify LowerLimit and/or UpperLimit")
    # C# compares NaN filters as false; unset filters are NaN, so these only fire when set
    if _ge(lf, ll):
        errors.add_general_error("LowerFilter must be less than LowerLimit")
    if _le(uf, ul):
        errors.add_general_error("UpperFilter must be greater than UpperLimit")


class LotLimit(Limit):
    BIN_GROUP = True

    def rule_specific(self, attrs, errors):
        Attr.integer("MinQuantity", attrs, errors, False, "LotLimit")
        mp = Attr.double("MinPercentage", attrs, errors, False, "LotLimit")
        mp = 0.0 if math.isnan(mp) else mp
        if mp < 0 or mp > 1:
            errors.add_general_error("MinPercentage must be between 0 and 1.")
        inc = self.include_bins is not None and self.include_bins.valid
        exc = self.exclude_bins is not None and self.exclude_bins.valid
        if not inc and not exc:
            errors.add_general_error("Must specify at least IncludeBins and/or ExcludeBins")
        Attr.boolean("ResetAtTrigger", attrs, errors, False, "LotLimit")
        Attr.boolean("ResetAtSummary", attrs, errors, False, "LotLimit")
        tq = Attr.integer("TriggerQuantity", attrs, errors, False, "LotLimit")
        tq = 0 if tq == DEFAULT_INT else tq
        tp = Attr.double("TriggerPercentage", attrs, errors, False, "LotLimit")
        tp = 0.0 if math.isnan(tp) else tp
        if tp < 0 or tp > 1:
            errors.add_general_error("TriggerPercentage must be between 0 and 1.")
        if tq <= 0 and tp <= 0:
            errors.add_general_error("Must specify at least one of TriggerQuantity and TriggerPercentage")


class Parametric(Limit):
    BIN_GROUP = True
    RULE_REFERENCE_PROPS = ("ParameterName",)

    def rule_specific(self, attrs, errors):
        n = "Parametric"
        uc = Attr.integer("UnitCount", attrs, errors, False, n)
        uc = 1 if uc <= 0 else uc
        stat = Attr.enum("ParametricStat", "Stat", attrs, errors, False, n)
        if stat == "UNSPECIFIED" and uc > 1:
            errors.add_general_error("Stat must be specified if UnitCount is specified and > 1")
        elif stat == "UNSPECIFIED":
            stat = "MEAN"
        q = Attr.double("Quantile", attrs, errors, False, n)
        if stat == "QUANTILE" and (math.isnan(q) or q <= 0 or q >= 1):
            errors.add_general_error("Quantile needs to be between 0 and 1 if Stat = Quantile")
        _parametric_limits(attrs, errors, n)
        pname = Attr.parametric_spec("ParameterName", attrs, errors, True, n)
        if pname is None:
            errors.add_general_error("Parametric rule needs to have a valid ParameterName")


RULES = {
    "XOFYPARAMETRIC": lambda: XofY("XofYParametric"),
    "XOFYBIN": lambda: XofY("XofYBin"),
    "LOTLIMIT": LotLimit,
    "PARAMETRIC": Parametric,
}
# Rule node names known to the original parser but not ported here.
UNPORTED_RULES = {"CONSECUTIVE", "DOUBLESTACK", "EFUSEDUPLICATION", "PARAMETRICEWMA", "BINEWMA",
                  "BINSWITCHEWMA", "PERIODICCOUNTER", "PERIODICTIME", "VALIDBIN", "PARAMETRICDIFFERENCE",
                  "CHISQUARED", "CHISQUAREDGENERIC", "VELOCITY", "DTSTRIM", "CHISQUAREDPARAMETRIC",
                  "KOLMOGOROVSMIRNOV"}
ACTIONS = {"ALARM", "REPEATTRIGGERGROUP", "CLEAN", "INLINEDIAG", "RETEST", "NOACTION"}


# ---------------------------------------------------------------- parser
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _attrs(elem):
    return OrderedDict((_local(k), v) for k, v in elem.attrib.items())


def _add_attributes(target, source, errors):
    for k, v in source.items():
        if k == "ConfigKey":
            continue
        if k in ("RuleName", "ClearScope") and len(v) > 60:
            errors.add_general_error(f"Found {k} has more than 60 characters.")
        if k == "BinOrToken" and len(v) > 30:
            errors.add_general_error(f"Found {k} has more than 30 characters.")
        if k in target:
            errors.add_general_error(f"Found two attributes with the same name: {k}")
        else:
            target[k] = v


def _add_elements(target, elem, errors):
    for c in elem:
        if not isinstance(c.tag, str):
            continue
        name = _local(c.tag)
        if name in target:
            errors.add_general_error(f"Found two elements/attributes with the same name: {name}")
        elif len(c) == 0 and not c.attrib:
            target[name] = c.text or ""
        else:
            target[name] = xlinq(c)


class ConfigGroups(dict):
    pass


def _extract_config_groups(element, target, groups, errors):
    """Copies attributes/elements of the referenced ConfigGroups from `element`'s ConfigKey onto `target`."""
    key_attr = element.get("ConfigKey")
    if key_attr is None:
        return
    for key in key_attr.split("|"):
        if key not in groups:
            errors.add_general_error(f"Failed to locate the ConfigGroup for Key: {key}")
            continue
        g = groups[key]
        for k, v in g.attrib.items():
            if k != "Key":
                target.set(k, v)
        for c in list(g):
            target.append(copy.deepcopy(c))


class RecipeData:
    def __init__(self):
        self.overflow_tokens = []
        self.alarms = []
        self.rules = []


def check_recipe_string(path, text, alarm_map=None, skip_workflow_check=False):
    """Returns (RecipeData | None, RecipeErrorTracker)."""
    errors = RecipeErrorTracker()
    try:
        root = ET.fromstring(text)
        if _local(root.tag) != "ComponentRecipe":
            raise ValueError(f"unexpected root element <{_local(root.tag)}> (expected ComponentRecipe)")
    except Exception as e:
        errors.add_general_error(f"Failed to deserialize recipe: {path}. Exception: {e}")
        return None, errors

    data = RecipeData()
    ot = root.find("OverflowTokens")
    tokens = None
    if ot is not None:
        tokens = [{"name": t.get("name") or "", "id": t.get("id") or ""} for t in ot.findall("OverflowToken")]
        data.overflow_tokens = tokens

    identity = root.find("Identity")
    if identity is None or not (identity.get("datagroup") or "").strip():
        errors.add_general_error("Missing or empty datagroup attribute in Identity node.")

    groups = ConfigGroups()
    sc = root.find("SharedConfigurations")
    if sc is not None:
        for g in sc.findall("ConfigGroup"):
            key = g.get("Key") or ""
            if not key:
                errors.add_general_error("Found ConfigGroup must specify a non empty Key attribute")
            elif key in groups:
                errors.add_general_error(f"Found duplicate ConfigurationGroup key: {key}")
            else:
                groups[key] = g

    known_root = {"BeginChecksum", "Identity", "Template", "SharedConfigurations", "OverflowTokens",
                  "RuleGroup", "ModuleRecipeToken", "CatalystPassThroughDefinition"}
    for rg in root.findall("RuleGroup"):
        _parse_rule_group(rg, groups, alarm_map, skip_workflow_check, errors, data)
    for child in root:
        if isinstance(child.tag, str) and _local(child.tag) not in known_root:
            _parse_rule(copy.deepcopy(child), groups, alarm_map, skip_workflow_check, errors, data)

    _setup_rule_dependencies(data, errors)
    _validate_overflow_tokens(data, alarm_map, errors)
    seen = {}
    for aid, msg in data.alarms:
        seen.setdefault(aid, msg)
    data.alarm_pairs = list(seen.items())
    return data, errors


RULEGROUP_ATTRS = {"GroupName", "ExcludeOperations", "ValidOperations", "ExcludeDevices", "ValidDevices",
                   "ExcludeFacilities", "ValidFacilities", "ExcludeProducts", "ValidProducts", "ValidPlatforms",
                   "ExcludeProcessSteps", "ValidProcessSteps", "ValidLotTypes", "ExcludeLotIds", "ValidLotIds"}


def _add_restriction_errors(rg, errors):
    a = rg.attrib
    for label in ("ProcessSteps", "Operations", "Devices", "Facilities", "Products", "LotIds"):
        if a.get("Valid" + label) and a.get("Exclude" + label):
            errors.add_general_error(f"Can not define both Valid{label} and Exclude{label}")


def _parse_rule_group(rg, groups, alarm_map, skip_wf, errors, data):
    if len([c for c in rg if isinstance(c.tag, str)]) == 0:
        return
    gname = rg.get("GroupName") or ""
    # Runs before MoveToNode, so (like the original) these errors attach to the previously visited node.
    _add_restriction_errors(rg, errors)
    group_attrs = OrderedDict((k, v) for k, v in rg.attrib.items() if k not in RULEGROUP_ATTRS)
    for child in [c for c in rg if isinstance(c.tag, str)]:
        _parse_rule(copy.deepcopy(child), groups, alarm_map, skip_wf, errors, data, gname, group_attrs)


def _parse_rule(rule, groups, alarm_map, skip_wf, errors, data, group="", group_attrs=None):
    errors.move_to_node(rule, None, group)
    name = _local(rule.tag)
    items = [c for c in rule if isinstance(c.tag, str)]
    if not items:
        errors.add_general_error(f"Found extra unknown node: {name}")
    for item in items:
        errors.move_to_node(rule, item, group)
        attrs = OrderedDict()
        _extract_config_groups(rule, item, groups, errors)
        _extract_config_groups(item, item, groups, errors)
        if _local(item.tag) == "RepeatTriggerGroup":
            for sub in [c for c in item if isinstance(c.tag, str)]:
                _extract_config_groups(sub, sub, groups, errors)
        if group_attrs:
            _add_attributes(attrs, group_attrs, errors)
        _add_attributes(attrs, _attrs(rule), errors)
        _add_attributes(attrs, _attrs(item), errors)
        action = _parse_action(rule, item, attrs, alarm_map, skip_wf, errors, data, group)
        key = name.upper()
        if key in RULES:
            errors.move_to_node(rule, item, group)
            limit = RULES[key]()
            limit.action = action
            refs = [v[2:-2] for k, v in attrs.items()
                    if k in INHERITABLE and v.startswith("{{") and v.endswith("}}")]
            limit.initialize_and_validate(attrs, errors)
            limit.refs = list(dict.fromkeys(refs))
            data.rules.append(limit)
            if attrs:
                errors.move_to_node(rule, None, group)
                for k, v in attrs.items():
                    errors.add_extra_attribute(k, v)
        elif key in UNPORTED_RULES:
            errors.add_general_error(f"Rule type {name} is valid in iLLIAD but is not implemented in this Python port")
        else:
            errors.add_general_error(f"Found unknown Limit Type with name: {name}")


INHERITABLE = ("ParameterName", "ParametricFilterTokenName", "MeasurementParameterName", "TargetParameterName")


def _parse_action(rule, child, attrs, alarm_map, skip_wf, errors, data, group=""):
    key = _local(child.tag).upper()
    if key not in ACTIONS:
        errors.add_general_error(f"Found unhandled action type: {_local(child.tag)}.  "
                                 "Only RaiseAlarm, Clean, and Retest are allowed in HDMx.")
        return None
    if key == "REPEATTRIGGERGROUP":
        rt = RepeatTrigger()
        base = OrderedDict(attrs)
        for sub in [c for c in child if isinstance(c.tag, str)]:
            sub_attrs = OrderedDict(base)
            errors.move_to_node(rule, sub, group)
            _add_attributes(sub_attrs, _attrs(sub), errors)
            act = _parse_action(rule, sub, sub_attrs, alarm_map, skip_wf, errors, data)
            rt.add_child(act, sub_attrs, errors)
            for k in list(attrs):
                if k not in sub_attrs:
                    del attrs[k]
        rt.initialize_and_validate(attrs, errors)
        return rt
    if len(child):
        _add_elements(attrs, child, errors)
    if key == "ALARM":
        alarm = Alarm(alarm_map, None, skip_wf)
        alarm.initialize_and_validate(attrs, errors)
        data.alarms.append((alarm.alarm_id, alarm.message))
        return alarm
    return object()


def _setup_rule_dependencies(data, errors):
    """{{RuleName}} references -> dependency graph; reports circular dependencies."""
    deps = {}
    for limit in data.rules:
        for parent in getattr(limit, "refs", ()):
            deps.setdefault(parent, [])
            deps.setdefault(limit.rule_name, [])
            if limit.rule_name not in deps[parent]:
                deps[parent].append(limit.rule_name)
    visited, done = set(), set()

    def visit(n):
        if n in visited:
            if n not in done:
                errors.add_general_error("Found circular dependency on rule " + n)
                raise RuntimeError("Cyclic dependency found")
            return
        visited.add(n)
        for d in deps[n]:
            visit(d)
        done.add(n)

    try:
        for n in sorted(deps, key=lambda k: len(deps[k])):
            visit(n)
    except RuntimeError as e:
        errors.add_general_error(f"Caught exception while building rule dependency graph: {e}")


def _validate_overflow_tokens(data, alarm_map, errors):
    toks = data.overflow_tokens or []
    ids = [t["id"] for t in toks]
    names = [t["name"] for t in toks]
    toks = [t for t in toks if ids.count(t["id"]) == 1 and names.count(t["name"]) == 1]
    if alarm_map is not None:
        reserved = set(alarm_map.parts["Action"]) | set(alarm_map.parts["BinOrToken"]) | set(alarm_map.parts["Rule"])
        reserved_ids = (set(alarm_map.parts["Action"].values()) | set(alarm_map.parts["BinOrToken"].values())
                        | set(alarm_map.parts["Rule"].values()))
        toks = [t for t in toks if t["name"] not in reserved and t["id"] not in reserved_ids]
    data.overflow_tokens = toks
    bad = []
    for t in toks:
        try:
            ok = MIN_BIN_OR_TOKEN <= int(t["id"]) <= MAX_BIN_OR_TOKEN
        except ValueError:
            ok = False
        if not ok:
            bad.append(t["id"])
    if bad:
        errors.add_general_error(f"OverflowTokens contained invalid ids [{','.join(bad)}] which are not "
                                 f"within the range of ({MIN_BIN_OR_TOKEN},{MAX_BIN_OR_TOKEN})")


# ---------------------------------------------------------------- viewer model
def next_valid_overflow_token(data, alarm_map):
    ids = [int(v) for v in alarm_map.parts["BinOrToken"].values()] if alarm_map else []
    ids += [int(t["id"]) for t in data.overflow_tokens if t["id"].isdigit() and int(t["id"]) < 30000]
    ids.sort()
    nxt = (ids[-1] if ids else -1) + 1
    if nxt <= 99998:
        return f"Next valid OverflowToken value is: {nxt:05d}\n"
    return ("ERROR: No more OverflowToken values available! Please check your current list "
            "to make sure overflow tokens <= 99998")


def validate_recipe_file(path, alarm_map=None, skip_workflow_check=False):
    """Equivalent of RecipeCheckerViewModel.ReadRecipeAndValidate -> ValidationText."""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    data, errors = check_recipe_string(path, text, alarm_map, skip_workflow_check)
    err = errors.get_accumulated_error_string()
    out = []
    if not err:
        out.append(f"\n  No errors found in recipe at : {path}")
        out.append(next_valid_overflow_token(data, alarm_map))
    else:
        out.append(err)
    return "\n".join(out), data, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="iLLIAD recipe checker (Python port)")
    ap.add_argument("recipe")
    ap.add_argument("--alarm-map", default=r"C:\DpcConfig\V1.00\AlarmIdMapping.xml")
    ap.add_argument("--skip-workflow-check", action="store_true",
                    help="do not require Workflow Path files to exist")
    ap.add_argument("--export-alarm-csv", metavar="FILE")
    a = ap.parse_args(argv)
    amap = AlarmIdMap.load(a.alarm_map)
    if amap is None:
        print(f"[warn] {a.alarm_map} not found; Rule/Action/BinOrToken lookups will fail", file=sys.stderr)
        amap = AlarmIdMap()
    text, data, errors = validate_recipe_file(a.recipe, amap, a.skip_workflow_check)
    print(text)
    if a.export_alarm_csv and data is not None:
        with open(a.export_alarm_csv, "w", encoding="utf-8") as f:
            f.write("AlarmId,Message\n")
            for aid, msg in data.alarm_pairs:
                f.write(f"{aid},{msg}\n")
    return 1 if errors.get_accumulated_error_string() else 0


if __name__ == "__main__":
    sys.exit(main())
