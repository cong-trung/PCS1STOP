"""Editable table model for PCS ``ComponentRecipe`` XML files (no UI code in here).

Three tables mirror the grids of the old WinForms tool:

    workflow    SharedConfigurations/ConfigGroup   Key, Workflow/@Path, Check (read-only: what a worker found out
                                                    about the Path - see ``set_path_checks``)
    parametric  PARAMETRIC rules                    GroupName, ParameterName, UpperLimit, Alarm Action / ConfigKey
    xofy        XOFYBIN rules                       ValidOperations, Bins, X, Y, RuleName, ClearScope,
                                                    Alarm Action / ConfigKey, preceding comment, Alarm BinOrToken,
                                                    (validation error, note - both read-only: what the validation
                                                    found out, see ``set_validation``)

Edits are applied to an lxml tree.  ``export_bytes`` then patches the *original text* in place, so only the
edited attribute values / comments - and the rows you added or deleted - change (CRLF vs LF, tabs, ``<a />``
style, BOM, XML declaration are all preserved).  The patched text is verified against the edited tree with C14N;
if anything looks off the model falls back to a plain lxml serialisation.

Cell rules
    * values are stripped; an empty cell removes the attribute (an XML attribute cannot be "half there")
    * XOFYBIN/PARAMETRIC attributes (Bins, X, Y, RuleName, ClearScope, ParameterName, UpperLimit) are read and
      edited where the file keeps them: on the rule, on its <RepeatTriggerGroup>, or on its <Alarm> children
    * Alarm columns (Action, ConfigKey, BinOrToken) span all <Alarm> children of a rule: one value applies to every
      alarm, ``a; b; c`` (one value per alarm, in order) edits them individually.  A list with more or fewer values
      than the rule has alarms changes the number of alarms: new ones are copies of the last alarm with the next
      Order, surplus ones are taken out from the end (one undo step, like every other edit)
    * RuleGroup-level columns (Rule Group Name, Valid Operations) change the whole group
    * ``set_flow`` (what the Action / Alarm Config Key pickers of the rule grid use, see ``flow.py``) gives a rule exactly
      the alarms it is told - ``(Action, ConfigKey)`` per alarm, written as they are, no ``;`` list - in one undo step.
      ``flow`` reads them back, and ``row_dict`` of an XOFYBIN rule carries them as the hidden ``_flow``: an alarm cell
      collapses equal values (three ``Socket`` alarms read ``Socket``), the flow does not

Rows
    ``add_row`` inserts a new XOFYBIN rule / workflow ConfigGroup right below a given row.  It is a copy of that
    row's structure with the identifying values cleared (RuleName, Bins, BinOrToken, ClearScope / Key, Path, no
    comment), so it follows the pcs_die_file's own conventions and the checker flags it until it is filled in.
    ``delete_row`` takes a row out of any table, together with the comment glued in front of it.
    Both are one undo step each (Ctrl+Z puts a deleted row back exactly where it was, or removes an added one).
    Whitespace "belongs" to the node behind it - a row's lead (the text in front of it) is added with it and
    dropped with it - so rows can be added and deleted in any order and the file stays tidy.
"""

from __future__ import annotations

import codecs
import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import PurePath

from lxml import etree

TABLES = ("workflow", "parametric", "xofy")
ADD_ROW_TABLES = ("workflow", "xofy")
DELETE_ROW_TABLES = TABLES

#: (field, header) per table, in display order.
COLUMNS: dict[str, list[tuple[str, str]]] = {
    "workflow": [("key", "Key"), ("path", "Path"), ("check", "Check")],
    "parametric": [
        ("group", "Rule Group Name"),
        ("parameter_name", "Parameter Name"),
        ("upper_limit", "Upper Limit"),
        ("action", "Action"),
        ("config_key", "Config Key"),
    ],
    "xofy": [
        ("valid_operations", "Valid Operations"),
        ("bins", "Bins"),
        ("x", "X"),
        ("y", "Y"),
        ("rule_name", "Rule Name"),
        ("clear_scope", "Clear Scope"),
        ("action", "Action"),
        ("alarm_config_key", "Alarm Config Key"),
        ("comment", "Comment"),
        ("bin_or_token", "BinOrToken"),
        ("error", "Validation Error"),
        ("note", "Note"),
    ],
}
READ_ONLY_FIELDS = frozenset({"error", "check", "note"})
ALARM_SEPARATOR = ";"
#: grid field -> attribute name, for the attributes a rule keeps on itself, its <RepeatTriggerGroup> or its alarms
RULE_ATTRS = {
    "parameter_name": "ParameterName",
    "upper_limit": "UpperLimit",
    "bins": "Bins",
    "x": "X",
    "y": "Y",
    "rule_name": "RuleName",
    "clear_scope": "ClearScope",
}
#: the columns that are made of the <Alarm>s of a rule - they read differently when an edit changes the number of alarms
#: (so do the RULE_ATTRS columns in a file that keeps those attributes on the alarms)
ALARM_COLUMNS = frozenset({"action", "alarm_config_key", "config_key", "bin_or_token"})
#: what the undo entry of ``set_flow`` calls its field: it changes the Action and the ConfigKey of the alarms at once
_FLOW = "flow"
_WS = " \t\r\n"

_XML_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_PARSER_ARGS = {
    "resolve_entities": False,
    "no_network": True,
    "load_dtd": False,
    "huge_tree": True,
    "remove_blank_text": False,
    "remove_comments": False,
    "remove_pis": False,
}


class PcsDieFileError(Exception):
    """The uploaded file cannot be used as a pcs_die_file."""


@dataclass(frozen=True)
class CellEdit:
    table: str
    row_id: str
    field: str
    value: str


@dataclass(frozen=True)
class CellUpdate:
    table: str
    row_id: str
    field: str
    value: str


@dataclass(frozen=True)
class RowAdded:
    table: str
    row: dict  # row_dict of the new row
    index: int  # its position in the table (document order)


@dataclass(frozen=True)
class RowRemoved:
    table: str
    row_id: str


@dataclass
class EditResult:
    #: cells whose displayed value the client must (re)write - every touched cell with its final value
    updates: list[CellUpdate] = field(default_factory=list)
    #: rows the client must insert / drop (add_row, undo and redo of it)
    added: list[RowAdded] = field(default_factory=list)
    removed: list[RowRemoved] = field(default_factory=list)
    applied: int = 0
    unchanged: int = 0
    skipped_readonly: int = 0
    skipped_unknown: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _Row:
    table: str
    id: str
    el: etree._Element
    ro: set[str] = field(default_factory=set)
    group: etree._Element | None = (
        None  # the RuleGroup it belongs to (parametric / xofy)
    )


@dataclass(frozen=True)
class _Change:
    table: str
    row_id: str
    field: str
    old: str
    new: str
    #: what the edit did to the attributes, ((element, attribute, before, after), ...) - undo / redo put exactly that
    #: back, on exactly those elements (None = no such attribute), whatever the cells look like by then.  An _AlarmOp
    #: stands in the same sequence where the edit put an alarm in or took one out
    attrs: tuple = ()


@dataclass
class _RowOp:
    """A row put into / taken out of the tree (add_row = ``added``, delete_row = not ``added``).

    Undo and redo run the op in one direction or the other, so a row always comes back with the very same elements,
    id, position and whitespace.  ``lead`` is the text in front of the row's first node (``ref.tail``, or
    ``parent.text`` when the row is the first child) while the row is in the tree, ``before`` the same slot while it
    is out; ``tail`` / ``gap`` are the element's / the glued comment's own tail."""

    added: bool
    row: _Row
    parent: etree._Element
    ref: (
        etree._Element | None
    )  # node in front of the row (None: the row is the parent's first child)
    nodes: list  # [glued comment,] row element
    lead: str | None
    gap: str | None
    tail: str | None
    before: str | None
    index: int  # position of the row in its table
    error: str | None = None  # validation message the row had when it was deleted
    note: str | None = None  # text of its Note cell when it was deleted


@dataclass
class _AlarmOp:
    """An <Alarm> put into / taken out of a rule because an alarm column got a list with more / fewer values than the
    rule has alarms (``added`` = put in).  It rides along in the attribute changes of that edit (``_Change.attrs``), so
    undo and redo run it in order with them and an alarm always comes back as the very same element in the same place.
    ``lead`` is the text in front of the alarm (``ref.tail``, or ``parent.text`` when it is the first child) while it
    is in the tree, ``before`` the same slot while it is out; ``tail`` is the alarm's own tail."""

    added: bool
    el: etree._Element
    parent: etree._Element
    ref: etree._Element | None  # node in front of the alarm (None: it is the first child of its parent)
    lead: str | None
    before: str | None
    tail: str | None

    def run(self, *, forward: bool) -> None:
        """Do the op (forward) or take it back."""
        if self.added == forward:
            self._link()
        else:
            self._unlink()

    def _link(self) -> None:
        if self.ref is not None:
            self.ref.addnext(self.el)
            self.ref.tail = self.lead
        else:
            self.parent.insert(0, self.el)
            self.parent.text = self.lead
        self.el.tail = self.tail

    def _unlink(self) -> None:
        self.parent.remove(self.el)  # takes the alarm's tail along
        if self.ref is not None:
            self.ref.tail = self.before
        else:
            self.parent.text = self.before


# ------------------------------------------------------------------------------------------ xml helpers
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _is(node, name: str) -> bool:
    return (
        node is not None
        and isinstance(node.tag, str)
        and _local(node.tag).upper() == name.upper()
    )


def _kids(el, name: str) -> list:
    u = name.upper()
    return [c for c in el if isinstance(c.tag, str) and _local(c.tag).upper() == u]


def _first(el, name: str):
    k = _kids(el, name)
    return k[0] if k else None


def _rulegroup(el):
    parent = el.getparent()
    return parent if _is(parent, "RuleGroup") else None


def _alarms(rule) -> list:
    """<Alarm> children of a rule, directly or inside its <RepeatTriggerGroup>, in document order."""
    out: list = []
    for c in rule:
        if not isinstance(c.tag, str):
            continue
        n = _local(c.tag).upper()
        if n == "ALARM":
            out.append(c)
        elif n == "REPEATTRIGGERGROUP":
            out.extend(_kids(c, "Alarm"))
    return out


def _next_order(alarms: list) -> int:
    """The Order after the highest of ``alarms`` (the alarms of a <RepeatTriggerGroup> count 1, 2, 3 ... without gaps)."""
    top = 0
    for a in alarms:
        try:
            top = max(top, int(_attr(a, "Order")))
        except ValueError:  # no Order, or one that is no number: the checker has its say
            pass
    return top + 1


def _ci_key(el, name: str):
    """Actual attribute key matching ``name`` case-insensitively (the checker is case-insensitive too)."""
    if name in el.attrib:
        return name
    low = name.lower()
    for k in el.attrib:
        if isinstance(k, str) and k.lower() == low:
            return k
    return None


def _attr(el, name: str) -> str:
    k = _ci_key(el, name)
    return el.get(k) if k is not None else ""


def _comment_of(el):
    """The comment glued in front of ``el`` (previous sibling, nothing but whitespace in between)."""
    p = el.getprevious()
    if p is not None and p.tag is etree.Comment and (p.tail or "").strip() == "":
        return p
    return None


def _display_comment(text: str | None) -> str:
    return " ".join((text or "").split())


def _alarm_display(values: list[str]) -> str:
    if not values:
        return ""
    if all(v == values[0] for v in values):
        return values[0]
    return (ALARM_SEPARATOR + " ").join(values)


def _remove_keep_tail(node) -> None:
    parent, tail = node.getparent(), node.tail
    if tail:
        prev = node.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(node)


def _detect_encoding(data: bytes) -> tuple[str, bytes]:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8", b"\xef\xbb\xbf"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16", data[:2]
    m = re.match(
        rb"<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._\-]+)[\"']", data[:300]
    )
    enc = m.group(1).decode("ascii") if m else "utf-8"
    try:
        codecs.lookup(enc)
    except LookupError:
        enc = "utf-8"
    return enc, b""


def _c14n(tree) -> bytes:
    return etree.tostring(tree, method="c14n", with_comments=True)


# --------------------------------------------------------------------------------- text-level scanner
_ATTR_RE = re.compile(r"""(\s+)([^\s=/>]+)(\s*=\s*)("([^"]*)"|'([^']*)')""")
_TOKEN_RE = re.compile(
    r"""
    (?P<comment><!--(?P<ctext>.*?)-->)
  | (?P<cdata><!\[CDATA\[.*?\]\]>)
  | (?P<pi><\?.*?\?>)
  | (?P<decl><![^>\[]*(?:\[.*?\]\s*)?>)
  | (?P<end></[^>]*>)
  | (?P<start><(?P<name>[^\s/>!?<]+)(?P<attrs>(?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(?P<selfclose>/)?>)
  | (?P<text>[^<]+)
""",
    re.DOTALL | re.VERBOSE,
)


class _PatchError(Exception):
    pass


class _Origin:
    """Where one element (start tag, end of the element, comment glued in front of it) sits in the original text."""

    __slots__ = (
        "attrs",
        "comment",
        "content",
        "end",
        "insert_at",
        "name",
        "name_end",
        "start",
    )

    def __init__(self):
        self.start = self.end = 0
        self.content: int | None = (
            None  # offset right behind the start tag (None: <self-closing/>)
        )
        self.name = ""
        self.name_end = 0
        #: raw attribute name -> (whitespace_start, value_start, value_end, quote_char)
        self.attrs: dict[str, tuple[int, int, int, str]] = {}
        self.insert_at = 0
        #: (comment_start, comment_end, text_start, text_end) of the comment immediately before, if any
        self.comment: tuple[int, int, int, int] | None = None


def _scan(text: str) -> list[_Origin]:
    pos = 0
    origins: list[_Origin] = []
    open_tags: list[_Origin] = []
    prev_comment: tuple[int, int, int, int] | None = None
    for m in _TOKEN_RE.finditer(text):
        if m.start() != pos:
            raise _PatchError(f"unscannable markup at offset {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "text":
            if m.group().strip():
                prev_comment = None
            continue
        if kind == "comment":
            prev_comment = (m.start(), m.end(), m.start("ctext"), m.end("ctext"))
            continue
        if kind == "start":
            o = _Origin()
            o.start, o.name = m.start(), m.group("name")
            o.name_end = o.insert_at = m.start("name") + len(o.name)
            base = m.start("attrs")
            for a in _ATTR_RE.finditer(m.group("attrs")):
                o.attrs[a.group(2)] = (
                    base + a.start(1),
                    base + a.start(4) + 1,
                    base + a.end(4) - 1,
                    a.group(4)[0],
                )
                o.insert_at = base + a.end(4)
            o.comment = prev_comment
            origins.append(o)
            if m.group("selfclose"):
                o.end = m.end()
            else:
                o.content = m.end()
                open_tags.append(o)
        elif kind == "end":
            if not open_tags:
                raise _PatchError(f"unbalanced end tag at offset {m.start()}")
            open_tags.pop().end = m.end()
        prev_comment = None
    if pos != len(text) or open_tags:
        raise _PatchError(f"unscannable markup at offset {pos}")
    return origins


def _empty_styles(text: str) -> tuple[dict[str, str], str]:
    """How the file writes elements without content: ``<a></a>`` ("pair"), ``<a />`` ("sp"), ``<a/>`` ("tight").
    Returns (style per element name, style for names that are never empty in the file)."""
    votes: dict[str, dict[str, int]] = {}
    total = {"sp": 0, "tight": 0}
    open_name, open_end = "", -1
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "start":
            name = m.group("name")
            if m.group("selfclose"):
                style = "sp" if text[m.start("selfclose") - 1].isspace() else "tight"
                v = votes.setdefault(name, {})
                v[style] = v.get(style, 0) + 1
                total[style] += 1
                open_name, open_end = "", -1
            else:
                open_name, open_end = name, m.end()
        else:
            if (
                kind == "end"
                and m.start() == open_end
                and m.group()[2:-1].strip() == open_name
            ):
                v = votes.setdefault(open_name, {})
                v["pair"] = v.get("pair", 0) + 1
            open_name, open_end = "", -1
    styles = {name: max(v, key=v.get) for name, v in votes.items()}
    return styles, ("sp" if total["sp"] > total["tight"] else "tight")


def _restyle(snippet: str, styles: dict[str, str], default: str) -> str:
    """lxml writes every empty element as ``<a/>``; write them the way the file does."""

    def fix(m: re.Match) -> str:
        if m.lastgroup != "start" or not m.group("selfclose"):
            return m.group()
        name, attrs = m.group("name"), m.group("attrs")
        style = styles.get(name, default)
        if style == "pair":
            return f"<{name}{attrs}></{name}>"
        return f"<{name}{attrs} />" if style == "sp" else f"<{name}{attrs}/>"

    return _TOKEN_RE.sub(fix, snippet)


def _esc_attr(value: str, quote: str) -> str:
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = (
        value.replace('"', "&quot;") if quote == '"' else value.replace("'", "&apos;")
    )
    return value.replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")


# ------------------------------------------------------------------------------------------- the model
class PcsDieFileDoc:
    """A loaded pcs_die_file: tables for the UI, edit/undo/redo, and export."""

    def __init__(self, data: bytes, filename: str = "pcs_die_file.xml"):
        self.filename = filename or "pcs_die_file.xml"
        self._orig = bytes(data)
        self._enc, self._bom = _detect_encoding(self._orig)
        try:
            root = etree.fromstring(self._orig, etree.XMLParser(**_PARSER_ARGS))
        except etree.XMLSyntaxError as e:
            raise PcsDieFileError(f"Not well-formed XML: {e}") from e
        self.tree = root.getroottree()
        self.root = root
        self.export_mode = (
            ""  # "patch" | "serialize" | "original" after the last export
        )

        #: every element the file had, in document order (what the text scanner lines up with, even after deletions)
        self._originals = [e for e in root.iter() if isinstance(e.tag, str)]
        self._rows: dict[str, list[_Row]] = {t: [] for t in TABLES}
        self._by_id: dict[tuple[str, str], _Row] = {}
        self._group_rows: dict = {}
        self._errors: dict[str, str] = {}
        self._notes: dict[str, str] = {}  # XOFYBIN row id -> text of its Note cell (what the validation says about the rule)
        self._path_checks: dict[str, str] = {}  # workflow Path -> text of the Check column (not part of the pcs_die_file)
        self._attr_orig: dict[tuple, str | None] = {}
        self._comment_orig: dict = {}
        self._added: dict = {}  # element of every row made by add_row (also the undone ones) -> its _Row
        self._gone: dict = {}  # element of every row that is deleted right now -> its _Row
        self._new_alarms: set = set()  # <Alarm> elements made by a resize of the alarms of a rule (also the undone ones)
        self._cut_alarms: dict = {}  # <Alarm> element the file had and a resize took out (once) -> the parent it was in
        self._slots: set = set()  # (element, attribute) left empty on purpose in an added row: stays, never deleted
        self._journal: list | None = (
            None  # attribute changes of the edit that is running (see _Change.attrs)
        )
        self._comment_stash: dict = {}  # row element -> (comment node, lead, gap) of the comment cleared last
        self._next = {t: 0 for t in TABLES}
        self._undo: list[list] = []  # batches of _Change / _RowOp
        self._redo: list[list] = []
        self._rev = 0
        self._saved_rev = 0
        self._extract()

    # ------------------------------------------------------------------------------------ extraction
    def _extract(self) -> None:
        for el in self.root.iter():
            if not isinstance(el.tag, str):
                continue
            name = _local(el.tag).upper()
            if name == "CONFIGGROUP" and _is(el.getparent(), "SharedConfigurations"):
                table = "workflow"
            elif name == "PARAMETRIC":
                table = "parametric"
            elif name == "XOFYBIN":
                table = "xofy"
            else:
                continue
            self._register(_Row(table, self._new_id(table), el))

    def _new_id(self, table: str) -> str:
        rid = f"{table[0]}{self._next[table]}"
        self._next[table] += 1
        return rid

    def _register(self, row: _Row, index: int | None = None) -> None:
        """Put a row (read from the file, or just inserted) into the tables."""
        el, t = row.el, row.table
        row.ro = set()
        if t == "workflow":
            if _first(el, "Workflow") is None:
                row.ro.add("path")
        else:
            row.group = _rulegroup(el)
            if row.group is None:
                row.ro.add("group" if t == "parametric" else "valid_operations")
            else:
                self._group_rows.setdefault(row.group, []).append(row)
            if not _alarms(el):
                row.ro.update(
                    {"config_key", "action"}
                    if t == "parametric"
                    else {"alarm_config_key", "action", "bin_or_token"}
                )
        rows = self._rows[t]
        rows.insert(len(rows) if index is None else index, row)
        self._by_id[(t, row.id)] = row

    def _unregister(self, row: _Row) -> None:
        rows = self._rows[row.table]
        del rows[next(i for i, r in enumerate(rows) if r is row)]
        del self._by_id[(row.table, row.id)]
        if row.group is not None:
            self._group_rows[row.group] = [
                r for r in self._group_rows.get(row.group, []) if r is not row
            ]
        self._errors.pop(row.id, None)
        self._notes.pop(row.id, None)

    # ---------------------------------------------------------------------------------------- reading
    def rows(self, table: str) -> list[dict]:
        return [self.row_dict(r) for r in self._rows[table]]

    def row_dict(self, row: _Row) -> dict:
        d = {"id": row.id}
        for f, _ in COLUMNS[row.table]:
            d[f] = self._get(row, f)
        d["_ro"] = sorted(row.ro)
        if row.table == "xofy":  # what the cell text cannot say: one value per alarm (see ``flow``)
            d["_flow"] = [list(step) for step in self._flow(row)]
        return d

    def info(self) -> dict:
        ident = _first(self.root, "Identity")
        out = {
            k: (ident.get(k, "") if ident is not None else "")
            for k in ("datagroup", "name", "version", "id", "asof")
        }
        out.update(
            root=_local(self.root.tag),
            checksum=self.root.get("checksum", ""),
            **{t: len(self._rows[t]) for t in TABLES},
        )
        return out

    def current_text(self) -> str:
        """The edited pcs_die_file as text (for the checker)."""
        return etree.tostring(self.root, encoding="unicode")

    def export_filename(self) -> str:
        p = PurePath(self.filename)
        return f"{p.stem or 'pcs_die_file'}_edited{p.suffix or '.xml'}"

    def is_editable(self, table: str, row_id: str, f: str) -> bool:
        row = self._by_id.get((table, row_id))
        return (
            row is not None
            and f not in row.ro
            and f not in READ_ONLY_FIELDS
            and any(f == c for c, _ in COLUMNS[table])
        )

    @staticmethod
    def _flow(row: _Row) -> list[tuple[str, str]]:
        return [(_attr(a, "Action"), _attr(a, "ConfigKey")) for a in _alarms(row.el)]

    def flow(self, table: str, row_id: str) -> list[tuple[str, str]] | None:
        """``(Action, ConfigKey)`` of every alarm of a rule, in order, as the tree has them right now - the alarm cells
        collapse equal values (three ``Socket`` alarms read ``Socket``), this does not.  None = no such rule, or a rule
        without alarms."""
        row = self._by_id.get((table, row_id))
        if row is None or table != "xofy" or not _alarms(row.el):
            return None
        return self._flow(row)

    def workflow_keys(self) -> list[str]:
        """The distinct, non-empty Keys of the List Workflow table in file order - the names a ConfigKey may use."""
        keys = (self._get(row, "key").strip() for row in self._rows["workflow"])
        return list(dict.fromkeys(k for k in keys if k))

    def _get(self, row: _Row, f: str) -> str:
        el, t = row.el, row.table
        if f == "error":
            return self._errors.get(row.id, "")
        if f == "note":
            return self._notes.get(row.id, "")
        if t == "workflow":
            if f == "key":
                return _attr(el, "Key")
            wf = _first(el, "Workflow")
            path = _attr(wf, "Path") if wf is not None else ""
            if f == "check":  # keyed by the path, so edits, undo and duplicate rows always show the right verdict
                return self._path_checks.get(path.strip(), "")
            return path
        if f == "comment":
            node = _comment_of(el)
            return _display_comment(node.text) if node is not None else ""
        if f in ("group", "valid_operations"):
            g = _rulegroup(el)
            return (
                _attr(g, "GroupName" if f == "group" else "ValidOperations")
                if g is not None
                else ""
            )
        if f in ("alarm_config_key", "config_key"):
            return _alarm_display([_attr(a, "ConfigKey") for a in _alarms(el)])
        if f == "action":
            return _alarm_display([_attr(a, "Action") for a in _alarms(el)])
        if f == "bin_or_token":
            return _alarm_display([_attr(a, "BinOrToken") for a in _alarms(el)])
        name = RULE_ATTRS[f]
        owner, alarms = self._scope(el, name)
        if owner is not None:
            return _attr(owner, name)
        return _alarm_display([_attr(a, name) for a in alarms]) if alarms else ""

    @staticmethod
    def _scope(el, name: str):
        """Where ``name`` lives for a rule.  pcs_die_file_checker merges the attributes of the rule, of its child
        (<RepeatTriggerGroup> or <Alarm>) and of the alarms below it, so pcs_die_files keep e.g. RuleName / ParameterName /
        UpperLimit on any of them.  Returns ``(owner, None)`` for the rule or its <RepeatTriggerGroup>,
        ``(None, alarms)`` when only the <Alarm>s carry it, ``(None, None)`` when nobody does."""
        for cand in [el] + _kids(el, "RepeatTriggerGroup")[:1]:
            if _ci_key(cand, name) is not None:
                return cand, None
        alarms = _alarms(el)
        if any(_ci_key(a, name) is not None for a in alarms):
            return None, alarms
        return None, None

    # --------------------------------------------------------------------------------------- writing
    @staticmethod
    def _normalize(f: str, value) -> str:
        s = _XML_BAD_CHARS.sub("", "" if value is None else str(value))
        if f == "comment":
            return " ".join(s.split()).replace("--", "- -")
        return s.strip()

    def _set_attr(self, el, name: str, value: str) -> None:
        key = _ci_key(el, name) or name
        before = el.get(key)
        self._attr_orig.setdefault((el, key), before)
        if value == "" and (el, key) not in self._slots:
            after = None
            if key in el.attrib:
                del el.attrib[key]
        else:
            after = value  # (an empty slot of an added row stays put, so undo / redo keep it where it was)
            el.set(key, value)
        if self._journal is not None and before != after:
            self._journal.append((el, key, before, after))

    @staticmethod
    def _put_attrs(attrs: tuple, *, forward: bool) -> None:
        """Redo (forward) or undo one edit's attribute changes - and the alarms it put in or took out - exactly."""
        for entry in attrs if forward else reversed(attrs):
            if isinstance(entry, _AlarmOp):
                entry.run(forward=forward)
                continue
            el, key, before, after = entry
            value = after if forward else before
            if value is None:
                if key in el.attrib:
                    del el.attrib[key]
            else:
                el.set(key, value)

    def _alarm_op(self, op: _AlarmOp) -> None:
        op.run(forward=True)
        if self._journal is not None:
            self._journal.append(op)

    def _resize_alarms(self, alarms: list, count: int) -> list:
        """Give a rule ``count`` alarms: the new ones are copies of the last alarm - numbered on, Order - and sit right
        behind it, the surplus ones are taken out from the end.  Returns the alarms the rule has now."""
        alarms = list(alarms)
        while len(alarms) < count:
            last = alarms[-1]
            parent = last.getparent()
            new = copy.deepcopy(last)
            new.tail = None
            order = _ci_key(new, "Order")
            if order is not None:
                new.set(order, str(_next_order(_kids(parent, "Alarm"))))
            self._slots.update((new, k) for k in new.attrib if (last, k) in self._slots)
            self._alarm_op(_AlarmOp(True, new, parent, last, self._lead(last), last.tail, last.tail))
            self._new_alarms.add(new)
            alarms.append(new)
        while len(alarms) > count:
            gone = alarms.pop()
            parent, ref = gone.getparent(), gone.getprevious()
            lead = ref.tail if ref is not None else parent.text
            self._alarm_op(_AlarmOp(False, gone, parent, ref, lead, gone.tail, gone.tail))
            if gone not in self._new_alarms:
                self._cut_alarms[gone] = parent
        return alarms

    def _set_alarms(self, alarms: list, name: str, value: str, *, resize: bool = False) -> None:
        """Write ``value`` to the alarms: the same one to every alarm, or - when it is a list, ``a; b; c`` - one value
        per alarm.  ``resize`` (the Action / ConfigKey / BinOrToken columns): a list of any other length first gives
        the rule as many alarms as there are values; without it such a list is written to every alarm as it is."""
        parts = [p.strip() for p in value.split(ALARM_SEPARATOR)]
        if resize and len(parts) > 1 and alarms:
            alarms = self._resize_alarms(alarms, len(parts))
        values = parts if len(alarms) > 1 and len(parts) == len(alarms) else [value] * len(alarms)
        for a, v in zip(alarms, values):
            self._set_attr(a, name, v)

    def _set_comment(self, el, text: str) -> None:
        node = _comment_of(el)
        if el not in self._comment_orig:
            self._comment_orig[el] = node.text if node is not None else None
        orig = self._comment_orig[el]
        if text == "":
            if node is not None:
                ref = node.getprevious()
                lead = (ref.tail if ref is not None else node.getparent().text) or ""
                self._comment_stash[el] = (node, lead, node.tail or "")
                _remove_keep_tail(node)
            return
        if orig is not None and _display_comment(orig) == text:
            body = orig  # back to what the file had: restore it byte for byte (padding, line breaks)
        elif node is None:
            body = f" {text} "
        else:
            m = re.match(r"(\s*)(.*?)(\s*)\Z", node.text or "", re.DOTALL)
            body = f"{m.group(1)}{text}{m.group(3)}"
        if body.endswith("-"):
            body += " "  # a comment may not end with '-'
        if node is None:
            self._insert_comment(el, body)
        else:
            node.text = body

    def _insert_comment(self, el, body: str) -> None:
        """Put a comment in front of ``el``.  One that was cleared a moment ago comes back as the same node with the
        whitespace around it as it was (a comment on a line of its own stays on its own line); otherwise it is new and
        sits right against the element."""
        stash = self._comment_stash.pop(el, None)
        if stash is not None:
            node, lead, gap = stash
            ref, parent = el.getprevious(), el.getparent()
            if (
                node.getparent() is None
                and ((ref.tail if ref is not None else parent.text) or "") == lead + gap
            ):
                node.text = body
                el.addprevious(node)
                if ref is not None:
                    ref.tail = lead or None
                else:
                    parent.text = lead or None
                node.tail = gap or None
                return
        el.addprevious(etree.Comment(body))

    def _set(self, row: _Row, f: str, value: str) -> None:
        el, t = row.el, row.table
        if t == "workflow":
            if f == "key":
                self._set_attr(el, "Key", value)
            elif f == "path":
                self._set_attr(_first(el, "Workflow"), "Path", value)
            else:
                raise ValueError(f"{f} cannot be edited")
        elif f == "comment":
            self._set_comment(el, value)
        elif f in ("group", "valid_operations"):
            self._set_attr(
                _rulegroup(el),
                "GroupName" if f == "group" else "ValidOperations",
                value,
            )
        elif f in ("alarm_config_key", "config_key"):
            self._set_alarms(_alarms(el), "ConfigKey", value, resize=True)
        elif f == "action":
            self._set_alarms(_alarms(el), "Action", value, resize=True)
        elif f == "bin_or_token":
            self._set_alarms(_alarms(el), "BinOrToken", value, resize=True)
        else:
            name = RULE_ATTRS[f]
            owner, alarms = self._scope(el, name)
            if alarms:
                self._set_alarms(alarms, name, value)
            else:
                self._set_attr(owner if owner is not None else el, name, value)

    def _siblings(self, row: _Row, f: str) -> list[_Row]:
        if f in ("group", "valid_operations"):
            g = _rulegroup(row.el)
            if g is not None:
                return [
                    r
                    for r in self._group_rows.get(g, [])
                    if r.table == row.table and r is not row
                ]
        return []

    # ------------------------------------------------------------------------------------- add a row
    @staticmethod
    def _lead(anchor) -> str:
        """Whitespace in front of ``anchor`` (and of the comment glued to it) - what separates two rows."""
        c = _comment_of(anchor)
        first = anchor if c is None else c
        prev = first.getprevious()
        ws = (prev.tail if prev is not None else first.getparent().text) or ""
        if ws.strip() == "":
            return ws
        m = re.search(
            r"\n[ \t]*\Z", ws
        )  # text between the siblings: keep only the indentation
        return m.group() if m else "\n"

    @staticmethod
    def _indent_of(el) -> str:
        prev, parent = el.getprevious(), el.getparent()
        ws = (
            prev.tail
            if prev is not None
            else (parent.text if parent is not None else "")
        ) or ""
        m = re.search(r"\n([ \t]*)\Z", ws)
        return m.group(1) if m else ""

    def _indent_unit(self) -> str:
        sample = self._orig[len(self._bom) : 300_000].decode(self._enc, "ignore")
        widths = []
        for line in sample.splitlines()[:400]:
            body = line.lstrip(" \t")
            if body and len(body) != len(line):
                if line[0] == "\t":
                    return "\t"
                widths.append(len(line) - len(body))
        return " " * (min(widths) if widths else 2)

    def _blank_rule(self, ref_el):
        """Copy of an XOFYBIN rule with its structure and settings but not what identifies it: RuleName and Bins
        (wherever the file keeps them) and every alarm's BinOrToken are emptied, ClearScope is dropped (it is optional
        and must not be empty), and the comment is not copied."""
        new = copy.deepcopy(ref_el)
        new.tail = None
        scopes = [
            (n, ("RuleName", "Bins")) for n in [new, *_kids(new, "RepeatTriggerGroup")]
        ]
        scopes += [(a, ("RuleName", "Bins", "BinOrToken")) for a in _alarms(new)]
        for node, names in scopes:
            for name in names:
                key = _ci_key(node, name)
                if key is not None:
                    node.set(
                        key, ""
                    )  # stays where it is, so the attribute order / location of the file is kept
                    self._slots.add((node, key))
        for node in [new, *_kids(new, "RepeatTriggerGroup"), *_alarms(new)]:
            key = _ci_key(node, "ClearScope")
            if key is not None:
                del node.attrib[key]
        return new

    def _blank_workflow(self, template: _Row | None, anchor, parent, lead: str):
        """A ConfigGroup with an empty Workflow, laid out like ``template`` (or with the file's indentation)."""

        def qualified(tag, local: str) -> str:
            ns = etree.QName(tag).namespace
            return f"{{{ns}}}{local}" if ns else local

        if template is not None:
            cg0, wf0 = template.el, _first(template.el, "Workflow")
            new = etree.Element(cg0.tag)
            key = _ci_key(cg0, "Key")
            if key is not None:
                new.set(key, "")
                self._slots.add((new, key))
            wf = etree.SubElement(new, wf0.tag)
            path = _ci_key(wf0, "Path") or "Path"
            wf.set(path, "")
            self._slots.add((wf, path))
            new.text = cg0.text if (cg0.text or "").strip() == "" else None
            wf.tail = wf0.tail if (wf0.tail or "").strip() == "" else None
            return new
        cg_tag = (
            anchor.tag if anchor is not None else qualified(parent.tag, "ConfigGroup")
        )
        new = etree.Element(cg_tag, Key="")
        wf = etree.SubElement(new, qualified(cg_tag, "Workflow"), Path="")
        self._slots.update({(new, "Key"), (wf, "Path")})
        if lead:  # (a file without any line breaks gets no whitespace either)
            new.text, wf.tail = lead + self._indent_unit(), lead
        return new

    def _template_rule(self, ref: _Row) -> _Row:
        """The rule whose structure a new row copies: the one it is added below - or, when that one has no alarms (its
        Action / ConfigKey / BinOrToken cells would be dead), the nearest rule that has."""
        if _alarms(ref.el):
            return ref
        rows = self._rows["xofy"]
        i = next(k for k, r in enumerate(rows) if r is ref)
        for d in range(1, len(rows)):
            for j in (i - d, i + d):
                if 0 <= j < len(rows) and _alarms(rows[j].el):
                    return rows[j]
        return ref

    def add_row(self, table: str, after_id: str | None = None) -> EditResult:
        """Insert a new row right below ``after_id`` (default: below the last row of the table).

        xofy: a copy of that XOFYBIN rule with RuleName, Bins and BinOrToken emptied (same group, same layout)
        workflow: a ConfigGroup with an empty Workflow, after the given one
        The new row is one undo step; ``EditResult.added`` tells the UI where it went."""
        if table not in ADD_ROW_TABLES:
            raise PcsDieFileError(f"Rows cannot be added to the {table} table.")
        rows = self._rows[table]
        ref = self._by_id.get((table, after_id)) if after_id else None
        if ref is None and rows:
            ref = rows[-1]
        if ref is not None:
            anchor, parent = ref.el, ref.el.getparent()
            before, lead, tail = anchor.tail, self._lead(anchor), anchor.tail
        elif table == "xofy":
            raise PcsDieFileError(
                "This pcs_die_file has no XOFYBIN rule to copy the structure from."
            )
        else:
            anchor = None
            parent = next(
                (e for e in self.root.iter() if _is(e, "SharedConfigurations")), None
            )
            if parent is None:
                raise PcsDieFileError(
                    "This pcs_die_file has no <SharedConfigurations> section to add a workflow to."
                )
            pad = self._indent_of(parent)
            if (
                b"\n" in self._orig
            ):  # a file with line breaks: indent the new row like the others
                before, lead = parent.text, "\n" + pad + self._indent_unit()
                tail = (
                    parent.text
                    if parent.text and parent.text.strip() == ""
                    else "\n" + pad
                )  # closing tag stays put
            else:  # everything on one line: no whitespace at all
                before, lead, tail = parent.text, "", parent.text

        if table == "xofy":
            new = self._blank_rule(self._template_rule(ref).el)
        else:
            gone = [
                r for r in self._gone.values() if r.table == table
            ]  # a deleted row still shows how rows look
            candidates = ([ref] if ref is not None else []) + rows + gone
            template = next(
                (r for r in candidates if _first(r.el, "Workflow") is not None), None
            )
            new = self._blank_workflow(template, anchor, parent, lead)
        row = _Row(table, self._new_id(table), new)
        index = (
            len(rows)
            if ref is None
            else next(i for i, r in enumerate(rows) if r is ref) + 1
        )
        op = _RowOp(True, row, parent, anchor, [new], lead, None, tail, before, index)
        self._link(op)
        self._added[new] = row
        self._undo.append([op])
        self._redo.clear()
        self._rev += 1
        res = EditResult(applied=1)
        res.added.append(RowAdded(table, self.row_dict(row), index))
        return res

    def delete_row(self, table: str, row_id: str) -> EditResult:
        """Take a row out of the tree - with the comment glued in front of it and the whitespace that led up to it.
        One undo step: Ctrl+Z puts everything back exactly where it was."""
        if table not in DELETE_ROW_TABLES:
            raise PcsDieFileError(f"Rows cannot be deleted from the {table} table.")
        row = self._by_id.get((table, row_id))
        if row is None:
            raise PcsDieFileError("That row does not exist any more.")
        op = _RowOp(False, row, row.el.getparent(), None, [], None, None, None, None, 0)
        self._unlink(op)
        self._undo.append([op])
        self._redo.clear()
        self._rev += 1
        res = EditResult(applied=1)
        res.removed.append(RowRemoved(table, row.id))
        return res

    def _set_lead(self, op: _RowOp, ws: str | None) -> None:
        if op.ref is not None:
            op.ref.tail = ws
        else:
            op.parent.text = ws

    def _link(self, op: _RowOp) -> None:
        """Put the row into the tree (add_row, redo of it, undo of delete_row)."""
        prev = op.ref
        for n in op.nodes:
            if prev is not None:
                prev.addnext(n)
            else:
                op.parent.insert(0, n)
            prev = n
        self._set_lead(op, op.lead)
        if len(op.nodes) == 2:
            op.nodes[0].tail = op.gap
        op.nodes[-1].tail = op.tail
        self._register(op.row, op.index)
        self._gone.pop(op.row.el, None)
        if op.error is not None:
            self._errors[op.row.id] = op.error
        if op.note is not None:
            self._notes[op.row.id] = op.note

    def _unlink(self, op: _RowOp) -> None:
        """Take the row out of the tree (delete_row, redo of it, undo of add_row).  What the op remembers for putting it
        back is read from the tree right now: a comment edit that was undone and redone in between has made a new
        comment node, so nothing recorded earlier can be trusted to still be there."""
        row = op.row
        el = row.el
        comment = _comment_of(el)
        first = el if comment is None else comment
        op.parent = el.getparent()
        op.ref = first.getprevious()
        op.nodes = [el] if comment is None else [comment, el]
        op.lead = (op.ref.tail if op.ref is not None else op.parent.text) or None
        op.gap = comment.tail if comment is not None else None
        op.tail = el.tail
        if not op.added:  # (an added row goes back to the exact text it was added to)
            keep = (op.lead or "").rstrip(
                _WS
            )  # text that is not whitespace (never in practice) is not ours to drop
            op.before = (keep + (op.tail or "")) or None
        op.index = next(i for i, r in enumerate(self._rows[row.table]) if r is row)
        op.error = self._errors.get(row.id)
        op.note = self._notes.get(row.id)
        self._unregister(row)
        for n in op.nodes:
            op.parent.remove(n)  # takes the node's tail along
        self._set_lead(op, op.before)
        self._gone[el] = row

    # ------------------------------------------------------------------------------------- edit API
    def apply_edits(
        self, edits: Iterable[CellEdit], *, record: bool = True
    ) -> EditResult:
        res = EditResult()
        changes: list[_Change] = []
        touched: dict[tuple[str, str, str], None] = {}
        for e in edits:
            row = self._by_id.get((e.table, e.row_id))
            if row is None or not any(e.field == c for c, _ in COLUMNS[e.table]):
                res.skipped_unknown += 1
                continue
            touched[(e.table, e.row_id, e.field)] = None
            if e.field in row.ro or e.field in READ_ONLY_FIELDS:
                res.skipped_readonly += 1
                continue
            new, old = self._normalize(e.field, e.value), self._get(row, e.field)
            if new == old:
                res.unchanged += 1
                continue
            self._journal = []
            try:
                self._set(row, e.field, new)
            except (
                ValueError
            ) as ex:  # lxml refuses an invalid XML string: leave the cell as it was
                self._put_attrs(tuple(self._journal), forward=False)
                res.errors.append(str(ex))
                continue
            finally:
                attrs, self._journal = tuple(self._journal), None
            changes.append(
                _Change(e.table, e.row_id, e.field, old, self._get(row, e.field), attrs)
            )
            res.applied += 1
            if any(isinstance(x, _AlarmOp) for x in attrs):
                self._touch_alarm_cells(touched, row)
            for sib in self._siblings(row, e.field):
                touched[(sib.table, sib.id, e.field)] = None
        if changes:
            self._rev += 1
            if record:
                self._undo.append(changes)
                self._redo.clear()
        res.updates = self._collect(touched)
        return res

    def _touch_alarm_cells(self, touched: dict, row: _Row) -> None:
        """The number of alarms of a rule changed: every cell made of its alarms may read differently now."""
        for f, _ in COLUMNS[row.table]:
            if f in ALARM_COLUMNS or (
                f in RULE_ATTRS and self._scope(row.el, RULE_ATTRS[f])[1] is not None
            ):
                touched[(row.table, row.id, f)] = None

    def set_flow(
        self, table: str, row_id: str, steps: Iterable[tuple[str, str]]
    ) -> EditResult:
        """Give a rule exactly these alarms: step i = ``(Action, ConfigKey)`` of alarm i, an empty ConfigKey = the alarm
        has none.  The rule is resized once (new alarms are copies of the last alarm, surplus ones go from the end, as
        for the alarm cells) and every alarm gets its two values - in one go: one undo step, and when a write fails the
        rule stays as it was.  What an alarm carries besides (Order, BinOrToken, Severity ...) stays with its place.
        The values are written as they are - no ``;`` list is parsed, so a Key may contain one."""
        res = EditResult()
        row = self._by_id.get((table, row_id))
        if table != "xofy" or row is None:
            res.skipped_unknown += 1
            return res
        if "action" in row.ro:  # a rule without alarms
            res.skipped_readonly += 1
            return res
        wanted = [
            (self._normalize("action", a), self._normalize("alarm_config_key", k))
            for a, k in steps
        ]
        if not wanted:
            res.errors.append("A rule needs at least one alarm.")
            return res
        alarms = _alarms(row.el)
        if len(wanted) != len(alarms) and len({a.getparent() for a in alarms}) > 1:
            res.errors.append(
                "The alarms of this rule are not all in one place, so their number cannot be changed here - "
                "edit the Action cell as text."
            )
            return res
        if wanted == self._flow(row):
            res.unchanged += 1
            return res
        before = self._get(row, "action")
        self._journal = []
        try:
            alarms = self._resize_alarms(alarms, len(wanted))
            for a, (action, key) in zip(alarms, wanted):
                self._set_attr(a, "Action", action)
                self._set_attr(a, "ConfigKey", key)
        except ValueError as ex:  # lxml refuses an invalid XML string: the rule stays as it was
            self._put_attrs(tuple(self._journal), forward=False)
            res.errors.append(str(ex))
            return res
        finally:
            attrs, self._journal = tuple(self._journal), None
        self._rev += 1
        self._undo.append(
            [_Change(table, row_id, _FLOW, before, self._get(row, "action"), attrs)]
        )
        self._redo.clear()
        res.applied = 1
        touched: dict[tuple[str, str, str], None] = {}
        self._touch_alarm_cells(touched, row)
        res.updates = self._collect(touched)
        return res

    def _collect(self, touched) -> list[CellUpdate]:
        out: dict[tuple[str, str, str], CellUpdate] = {}
        for t, r, f in touched:
            row = self._by_id.get((t, r))
            if row is None:  # a row that an undo just took out has nothing to show
                continue
            out[(t, r, f)] = CellUpdate(t, r, f, self._get(row, f))
            if t == "workflow" and f == "path":  # the Check cell follows the Path it describes
                out[(t, r, "check")] = CellUpdate(t, r, "check", self._get(row, "check"))
        return list(out.values())

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def dirty(self) -> bool:
        return self._rev != self._saved_rev

    @property
    def revision(self) -> int:
        """Changes with every edit, add, delete, undo and redo - lets the UI tell whether a check is still current."""
        return self._rev

    def undo(self) -> EditResult | None:
        if not self._undo:
            return None
        batch = self._undo.pop()
        res = self._replay(batch, reverse=True)
        self._redo.append(batch)
        return res

    def redo(self) -> EditResult | None:
        if not self._redo:
            return None
        batch = self._redo.pop()
        res = self._replay(batch, reverse=False)
        self._undo.append(batch)
        return res

    def _replay(self, batch: list, *, reverse: bool) -> EditResult:
        res = EditResult()
        touched: dict[tuple[str, str, str], None] = {}
        for ch in reversed(batch) if reverse else batch:
            if isinstance(ch, _RowOp):
                if (
                    ch.added != reverse
                ):  # redo of an add / undo of a delete: the row comes (back)
                    self._link(ch)
                    res.added.append(
                        RowAdded(ch.row.table, self.row_dict(ch.row), ch.index)
                    )
                else:
                    self._unlink(ch)
                    res.removed.append(RowRemoved(ch.row.table, ch.row.id))
                res.applied += 1
                continue
            row = self._by_id[(ch.table, ch.row_id)]
            if ch.field == "comment":
                self._set(row, "comment", ch.old if reverse else ch.new)
            else:
                self._put_attrs(ch.attrs, forward=not reverse)
            if ch.field == _FLOW:  # (not a column: the Action and the ConfigKey of the alarms changed together)
                self._touch_alarm_cells(touched, row)
            else:
                touched[(ch.table, ch.row_id, ch.field)] = None
            if any(isinstance(x, _AlarmOp) for x in ch.attrs):
                self._touch_alarm_cells(touched, row)
            for sib in self._siblings(row, ch.field):
                touched[(sib.table, sib.id, ch.field)] = None
            res.applied += 1
        self._rev += 1
        res.updates = self._collect(touched)
        return res

    # ------------------------------------------------------------------------------------ path checks
    def workflow_paths(self) -> list[str]:
        """The distinct, non-empty Paths of the List Workflow table in file order (what "Check paths" sends out)."""
        paths = (self._get(row, "path").strip() for row in self._rows["workflow"])
        return list(dict.fromkeys(p for p in paths if p))

    def set_path_checks(self, checks: dict[str, str] | None) -> list[CellUpdate]:
        """Remember what the workers found out about workflow paths (path -> text of the Check column).

        Replaces every earlier verdict and returns the Check cells whose text changed.  This is information about the
        machine, not an edit of the pcs_die_file: no undo step, the document does not become dirty, export is unaffected."""
        rows = self._rows["workflow"]
        before = [self._get(row, "check") for row in rows]
        self._path_checks = {p.strip(): text for p, text in (checks or {}).items() if p and p.strip()}
        return [
            CellUpdate("workflow", row.id, "check", now)
            for row, old in zip(rows, before)
            if (now := self._get(row, "check")) != old
        ]

    # ------------------------------------------------------------------------------------ validation
    def bin_rules(self) -> list[tuple[str, str, str]]:
        """(row id, RuleName, Bins) of every XOFYBIN rule in file order - what the duplicate check compares."""
        return [(row.id, self._get(row, "rule_name"), self._get(row, "bins")) for row in self._rows["xofy"]]

    def set_validation(
        self,
        per_rule: dict[str, list[str]] | None,
        notes: dict[str, str] | None = None,
        *,
        checked: bool = True,
    ) -> list[CellUpdate]:
        """Store what the validation found out: the checker's messages (RuleName -> lines, the 'error' column) and the
        notes of the duplicate check (row id -> text, the 'note' column).  Returns the changed 'error' and 'note' cells
        of the XOFYBIN table."""
        per_rule = per_rule or {}
        before, before_notes = dict(self._errors), self._notes
        self._errors, self._notes = {}, {}
        if checked:
            for row in self._rows["xofy"]:
                name = self._get(row, "rule_name")
                errs = per_rule.get(name, []) if name else self._unnamed_errors(row)
                self._errors[row.id] = " | ".join(dict.fromkeys(errs)) if errs else "OK"
            self._notes = {rid: text for rid, text in (notes or {}).items() if text and ("xofy", rid) in self._by_id}
        ids = sorted({r.id for r in self._rows["xofy"]}, key=lambda s: int(s[1:]))
        return [
            CellUpdate("xofy", rid, "error", self._errors.get(rid, ""))
            for rid in ids
            if before.get(rid, "") != self._errors.get(rid, "")
        ] + [
            CellUpdate("xofy", rid, "note", self._notes.get(rid, ""))
            for rid in ids
            if before_notes.get(rid, "") != self._notes.get(rid, "")
        ]

    def _unnamed_errors(self, row: _Row) -> list[str]:
        """The checker reports a rule by its RuleName; a rule without one (a freshly added row) cannot be matched
        to its messages, so say what is missing here.  The full checker output stays in the Error tab."""
        errs = ["Rule Name is empty - set it, then Validate again for the full check"]
        for label, f in (("Bins", "bins"), ("X", "x"), ("Y", "y")):
            if not self._get(row, f):
                errs.append(f"{label} is empty")
        return errs

    # ------------------------------------------------------------------------------------------ export
    def _live_added(self) -> list:
        """Elements of the rows made by add_row that are in the tree right now."""
        return [el for el in self._added if el.getparent() is not None]

    def _gone_originals(self) -> list:
        """Elements of the rows the file had that are deleted right now."""
        return [el for el in self._gone if el not in self._added]

    def _alarms_changed(self) -> bool:
        """True while a resize of the alarms of a rule has put an alarm into the tree or taken one out of it."""
        return any(el.getparent() is not None for el in self._new_alarms) or any(
            el.getparent() is None for el in self._cut_alarms
        )

    def _new_alarms_in(self, index: dict, live: set) -> list:
        """The alarms a resize made that sit in the tree right now, in a rule the file already had (those in a row made
        by add_row are written with their row)."""
        return [el for el in self._new_alarms if el in live and el.getparent() in index]

    def _cut_alarms_in(self, index: dict, live: set) -> list:
        """The alarms the file had that a resize took out of a rule that is still there."""
        return [el for el, parent in self._cut_alarms.items() if el in index and el not in live and parent in live]

    def export_bytes(self) -> bytes:
        """The edited pcs_die_file, as the bytes to hand to the user."""
        if (
            not self._attr_orig
            and not self._comment_orig
            and not self._live_added()
            and not self._gone_originals()
            and not self._alarms_changed()
        ):
            out, mode = self._orig, "original"
        else:
            try:
                out, mode = self._export_patched(), "patch"
            except Exception:  # noqa: BLE001 - any trouble -> safe fallback
                out, mode = self._export_serialized(), "serialize"
        self.export_mode = mode
        self._saved_rev = self._rev
        return out

    def _export_serialized(self) -> bytes:
        had_decl = self._orig[len(self._bom) :].lstrip().startswith(b"<?xml")
        enc = "utf-16" if self._enc.startswith("utf-16") else self._enc
        out = etree.tostring(self.tree, encoding=enc, xml_declaration=had_decl or None)
        if self._bom and enc == "utf-8":
            out = self._bom + out
        if b"\r\n" in self._orig and not enc.startswith("utf-16"):
            out = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return out

    def _row_inserts(
        self, added: list, index: dict, origins: list[_Origin], text: str, eol: str
    ) -> dict[int, str]:
        """Text for every block of new rows: {offset in the original: markup}.  A block goes right behind the original
        element the new rows follow in the tree (or right behind the start tag of their parent when nothing precedes
        them), and consists of the whitespace the tree has in front of each node plus the new element(s) and the
        comment glued to them.  The original whitespace after that point then simply follows, exactly as the tree has
        it behind the last new row."""
        live = set(added)

        def owned(n) -> bool:
            return n in live or (
                n.tag is etree.Comment
                and n.getnext() in live
                and (n.tail or "").strip() == ""
            )

        runs: dict = {}  # (kind, node) -> (offset, parent, anchor element)
        for el in added:
            a = el.getprevious()
            while a is not None and owned(a):
                a = a.getprevious()
            if a is None:
                parent = el.getparent()
                if parent not in index or origins[index[parent]].content is None:
                    raise _PatchError("no room for the new row in the original file")
                runs[("start", parent)] = (origins[index[parent]].content, parent, None)
            elif isinstance(a.tag, str) and a in index:
                runs[("after", a)] = (origins[index[a]].end, None, a)
            else:
                raise _PatchError(
                    "new row does not follow an element of the original file"
                )

        styles, default = _empty_styles(text)

        def markup(node) -> str:
            if node.tag is etree.Comment:
                return f"<!--{node.text or ''}-->".replace("\n", eol)
            xml = etree.tostring(node, encoding="unicode", with_tail=False)
            return _restyle(xml, styles, default).replace("\n", eol)

        out: dict[int, str] = {}
        for pos, parent, a in runs.values():
            lead, node = (
                (parent.text, parent[0]) if a is None else (a.tail, a.getnext())
            )
            parts: list[str] = []
            while node is not None and owned(node):
                parts.append((lead or "").replace("\n", eol))
                parts.append(markup(node))
                lead, node = node.tail, node.getnext()
            out[pos] = "".join(parts)
        return out

    @staticmethod
    def _row_deletes(
        gone: list, index: dict, origins: list[_Origin], text: str, *, glued: bool = True
    ) -> list[tuple[int, int, str]]:
        """Text spans of the deleted rows: the row (with the comment glued to it - unless ``glued`` is off, for the
        alarms a resize took out, whose comments stay) and the whitespace in front of it."""
        spans = []
        for el in gone:
            o = origins[index[el]]
            p = o.comment[0] if glued and o.comment else o.start
            while p > 0 and text[p - 1] in _WS:
                p -= 1
            spans.append((p, o.end, ""))
        return spans

    def _export_patched(self) -> bytes:
        if (
            self._enc.lower()
            .replace("_", "-")
            .startswith(("utf-16", "utf-32", "utf16", "utf32"))
        ):
            raise _PatchError("wide encodings are serialised")
        text = self._orig[len(self._bom) :].decode(self._enc)
        origins = _scan(text)
        if len(origins) != len(self._originals):
            raise _PatchError("element count differs from scanner")
        index = {}
        for i, (o, e) in enumerate(zip(origins, self._originals)):
            if o.name.rsplit(":", 1)[-1] != _local(e.tag):
                raise _PatchError("element order differs from scanner")
            index[e] = i
        live = set(self.root.iter())
        added = self._live_added() + self._new_alarms_in(index, live)
        eol = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"

        patches: list[tuple[int, int, str]] = []
        inserts: dict[int, list[str]] = {}
        for (el, key), orig in self._attr_orig.items():
            if (
                el not in index or el not in live
            ):  # a row that was added (written with the row) or deleted
                continue
            cur = el.get(key)
            if cur == orig:
                continue
            o = origins[index[el]]
            if key in o.attrs:
                ws, vs, ve, q = o.attrs[key]
                patches.append(
                    (ws, ve + 1, "") if cur is None else (vs, ve, _esc_attr(cur, q))
                )
            elif cur is not None:
                inserts.setdefault(o.insert_at, []).append(
                    f' {key}="{_esc_attr(cur, chr(34))}"'
                )
        for el, orig_text in self._comment_orig.items():
            if el not in index or el not in live:
                continue
            node = _comment_of(el)
            cur = node.text if node is not None else None
            if cur == orig_text:
                continue
            o = origins[index[el]]
            if orig_text is None:
                inserts.setdefault(o.start, []).append(
                    f"<!--{(cur or '').replace(chr(10), eol)}-->"
                )
            elif o.comment is None:
                raise _PatchError("comment not found in source")
            elif cur is None:
                patches.append((o.comment[0], o.comment[1], ""))
            else:
                patches.append((o.comment[2], o.comment[3], cur.replace("\n", eol)))
        # new rows first: at one offset they belong in front of a comment that is inserted for the next element
        if added:
            patches += [
                (pos, pos, chunk)
                for pos, chunk in self._row_inserts(
                    added, index, origins, text, eol
                ).items()
            ]
        patches += [(pos, pos, "".join(parts)) for pos, parts in inserts.items()]
        patches += self._row_deletes(
            [e for e in self._gone_originals() if e in index], index, origins, text
        )
        patches += self._row_deletes(
            self._cut_alarms_in(index, live), index, origins, text, glued=False
        )
        patches.sort(key=lambda p: (p[0], p[1]))
        for a, b in pairwise(patches):
            if a[1] > b[0]:
                raise _PatchError("overlapping patches")
        for start, end, new in reversed(patches):
            text = text[:start] + new + text[end:]

        out = self._bom + text.encode(self._enc)
        check = etree.fromstring(out, etree.XMLParser(**_PARSER_ARGS)).getroottree()
        if _c14n(check) != _c14n(self.tree):
            raise _PatchError("patched text does not match the edited tree")
        return out
