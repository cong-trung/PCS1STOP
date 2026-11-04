/* PCS Die File Checker - keyboard / clipboard layer for NiceGUI's AG Grid (Community edition).
 *
 * The server owns the data.  This file only
 *   1. tracks a rectangular cell range per grid (click, drag, Shift+click, Shift+arrows, Ctrl+A),
 *   2. turns Ctrl+C / Ctrl+X / Ctrl+V / Delete / Ctrl+Z / Ctrl+Y / Ctrl+S / Ctrl+O into events for the server,
 *   3. writes cell updates that the server sends back (pcs.apply) and shows rows it added / took out (pcs.rows),
 *   4. draws the trash icon of every row (pcs.trashCell) and asks the server to delete that row,
 *   5. is the cell editor (PickerEditor) of the Action and Alarm Config Key columns of the rule grid: a popup with a search
 *      box and the choices above the picked ones, which are dragged into order.  It does not write the cell: it sends the
 *      list (pcs_flow) and the server gives the rule exactly those alarms (pcs_die_file/flow.py).
 * Real text inputs (cell editors, dialogs, the file-path box) keep the browser's native shortcuts.
 */
(() => {
  'use strict';

  const TRASH = '_del'; // id of the trash-icon column: not a data column, never part of a selection / copy
  const READ_ONLY = ['error', 'note', 'check']; // computed by the validation / the workers, never editable
  const CONFIG_KEY = 'ConfigKey'; // the Action whose alarms run the workflows of a ConfigGroup (the alarm's ConfigKey)

  const S = (window.pcs = {
    editMode: false,
    product: 'SST', // SST | HST - set by the page (setProduct): what the pickers of the rule grid offer
    actions: [], // the Action names of AlarmIdMapping.xml - set by the page (setActions)
    picking: false, // true while a picker is open: its keys and pastes are its own, never the grid's
    picker: null, // the PickerEditor that is open
    uploadId: null,
    active: null, // key of the grid that was used last
    ranges: {}, // key -> {a: {r, c}, b: {r, c}}  (a = anchor, b = moving corner)
    dragging: null,
    shiftClick: false,
    muteFocus: 0,
    pasteSeen: false,
    pasteTimer: null,
    lastDelete: 0,
  });

  const ARROWS = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'];

  // ---------------------------------------------------------------- helpers
  const rootEl = (key) => document.querySelector(`[data-pcs="${key}"]`);
  const apiOf = (key) => {
    const el = rootEl(key);
    const comp = el && getElement(el);
    return comp ? comp.api : null;
  };
  const keyOf = (node) => {
    const el = node && node.closest ? node.closest('[data-pcs]') : null;
    return el ? el.dataset.pcs : null;
  };
  const allKeys = () => Array.from(document.querySelectorAll('[data-pcs]')).map((e) => e.dataset.pcs);
  const cols = (api) => api.getAllDisplayedColumns().filter((c) => c.getColId() !== TRASH);
  const colIndex = (api, colId) => cols(api).findIndex((c) => c.getColId() === colId);
  const nRows = (api) => api.getDisplayedRowCount();
  const isTextInput = (n) =>
    !!n && (n.tagName === 'INPUT' || n.tagName === 'TEXTAREA' || n.tagName === 'SELECT' || n.isContentEditable);
  const stop = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };
  const rectOf = (g) =>
    g && g.a && g.b
      ? {
          r1: Math.min(g.a.r, g.b.r),
          r2: Math.max(g.a.r, g.b.r),
          c1: Math.min(g.a.c, g.b.c),
          c2: Math.max(g.a.c, g.b.c),
        }
      : null;
  const refresh = (key) => {
    const api = apiOf(key);
    if (api) api.refreshCells({ force: true, suppressFlash: true });
  };
  const notice = (message, type) => emitEvent('pcs_notice', { message, type: type || 'info' });
  /** true (and a hint) when the grids are read-only */
  const readOnlyHint = (what) => {
    if (S.editMode) return false;
    notice(`Edit mode is off - press Edit (toolbar or button) to ${what || 'change cells'}.`, 'warning');
    return true;
  };
  /** (Action, ConfigKey) of every alarm of a rule row, as the server sends them (the cell texts collapse equal values) */
  const flowOf = (data) => (data && Array.isArray(data._flow) ? data._flow : []);
  const isSst = () => String(S.product).toUpperCase() !== 'HST';
  /** The cells the product keeps from being edited: SST has no Action to pick (it is always ConfigKey), and with HST the
   *  Alarm Config Key is for the rules that have a ConfigKey action. */
  const productAllows = (table, field, data) => {
    if (table !== 'xofy') return true;
    if (field === 'action') return !isSst();
    if (field === 'alarm_config_key') return isSst() || flowOf(data).some((s) => s[0] === CONFIG_KEY);
    return true;
  };
  /** Cells that text edits (paste, cut, Delete) must leave alone: the server refuses them too. */
  S.locked = (key, field) => key === 'xofy' && field === 'action' && isSst();

  /** Cell under a DOM node -> {r, c} in displayed order. */
  function cellAt(node, api) {
    const cell = node && node.closest ? node.closest('.ag-cell') : null;
    if (!cell) return null;
    const row = cell.closest('.ag-row');
    if (!row) return null;
    const r = parseInt(row.getAttribute('row-index'), 10);
    const c = colIndex(api, cell.getAttribute('col-id'));
    return r >= 0 && c >= 0 ? { r, c } : null;
  }

  /** Range of the grid, or its focused cell when no range exists yet. */
  function currentRect(key) {
    const g = S.ranges[key];
    if (g) return rectOf(g);
    const api = apiOf(key);
    const f = api && api.getFocusedCell();
    if (!f) return null;
    const c = colIndex(api, f.column.getColId());
    return c < 0 ? null : { r1: f.rowIndex, r2: f.rowIndex, c1: c, c2: c };
  }

  function ensureRange(key) {
    if (!S.ranges[key]) {
      const R = currentRect(key);
      if (!R) return null;
      S.ranges[key] = { a: { r: R.r1, c: R.c1 }, b: { r: R.r2, c: R.c2 } };
    }
    return S.ranges[key];
  }

  /** Editable-candidate cells of a rectangle as {id, field, value}. */
  function cellsOf(key, R, blank) {
    const api = apiOf(key);
    const cs = cols(api);
    const out = [];
    for (let r = R.r1; r <= R.r2; r++) {
      const node = api.getDisplayedRowAtIndex(r);
      if (!node) continue;
      for (let c = R.c1; c <= R.c2; c++) {
        const field = cs[c].getColId();
        if (READ_ONLY.includes(field) || S.locked(key, field)) continue;
        out.push({ id: node.id, field, value: blank ? '' : node.data ? node.data[field] : '' });
      }
    }
    return out;
  }

  function toTSV(key, R) {
    const api = apiOf(key);
    const cs = cols(api);
    const lines = [];
    for (let r = R.r1; r <= R.r2; r++) {
      const node = api.getDisplayedRowAtIndex(r);
      if (!node) continue;
      const vals = [];
      for (let c = R.c1; c <= R.c2; c++) {
        const v = node.data ? node.data[cs[c].getColId()] : '';
        vals.push(String(v == null ? '' : v).replace(/[\t\r\n]+/g, ' '));
      }
      lines.push(vals.join('\t'));
    }
    return lines.join('\n');
  }

  function parseTSV(text) {
    let t = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    if (t.endsWith('\n')) t = t.slice(0, -1);
    return t.split('\n').map((l) => l.split('\t'));
  }

  async function writeClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {
      /* fall through to the legacy path */
    }
    const prev = document.activeElement;
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
    S.muteFocus++;
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (_) {
      ok = false;
    }
    document.body.removeChild(ta);
    if (prev && prev.focus) prev.focus({ preventScroll: true });
    setTimeout(() => S.muteFocus--, 0);
    return ok;
  }

  // ------------------------------------------------------- hooks used by the grid options (Python side)
  S.isEditable = (p) =>
    S.editMode &&
    !READ_ONLY.includes(p.colDef.field) &&
    !(((p.data || {})._ro) || []).includes(p.colDef.field) &&
    productAllows(p.context && p.context.key, p.colDef.field, p.data);

  /** The page tells which product the Product box shows: the pickers (and which cells can be edited) follow it. */
  S.setProduct = (product) => {
    S.product = String(product || '').toUpperCase() === 'HST' ? 'HST' : 'SST';
    for (const key of allKeys()) refresh(key);
  };

  /** The page tells the Action names of AlarmIdMapping.xml: the choices of the Action picker. */
  S.setActions = (names) => {
    S.actions = Array.isArray(names) ? names.map(String) : [];
  };

  /** The Keys of the List Workflow table, distinct, in table order: the choices of the Alarm Config Key picker.  Read
   *  from the grid when the picker opens, so an edited, added or deleted key is there at once. */
  S.workflowKeys = () => {
    const api = apiOf('workflow');
    const keys = [];
    if (api) {
      api.forEachNode((node) => {
        const key = String((node.data && node.data.key) || '').trim();
        if (key && !keys.includes(key)) keys.push(key);
      });
    }
    return keys;
  };

  /** Look of a Check cell, from the text the server wrote (see services/path_checks.py): ok | bad | warn | wait | '' */
  S.checkKind = (text) => {
    const t = String(text == null ? '' : text);
    if (!t) return '';
    if (t.startsWith('OK')) return 'ok';
    if (t.startsWith('Not ')) return 'bad';
    if (t.startsWith('Failed') || t.startsWith('Timeout')) return 'warn';
    return 'wait';
  };

  S.inRange = (p) => {
    const R = rectOf(S.ranges[p.context && p.context.key]);
    if (!R || (R.r1 === R.r2 && R.c1 === R.c2)) return false; // one cell: AG Grid's own focus outline is enough
    const r = p.node.rowIndex;
    const c = colIndex(p.api, p.column.getColId());
    return r >= R.r1 && r <= R.r2 && c >= R.c1 && c <= R.c2;
  };

  // ---------------------------------------------------------------- actions (keyboard, toolbar)
  S.copy = async (cut) => {
    const key = S.active;
    const R = key && currentRect(key);
    if (!R) return;
    if (!(await writeClipboard(toTSV(key, R)))) return notice('The browser blocked clipboard access.', 'warning');
    if (cut && !readOnlyHint()) emitEvent('pcs_bulk', { table: key, kind: 'cut', cells: cellsOf(key, R, true) });
  };
  S.clear = (key) => {
    key = key || S.active;
    const R = key && currentRect(key);
    if (!R || readOnlyHint()) return;
    emitEvent('pcs_bulk', { table: key, kind: 'clear', cells: cellsOf(key, R, true) });
  };

  S.paste = (key, text) => {
    const api = apiOf(key);
    const R = currentRect(key);
    if (!api || !R || !text || readOnlyHint()) return;
    const M = parseTSV(text);
    const cs = cols(api);
    const n = nRows(api);
    const cells = [];
    const push = (r, c, value) => {
      const node = api.getDisplayedRowAtIndex(r);
      const field = cs[c].getColId();
      if (node && !READ_ONLY.includes(field) && !S.locked(key, field)) cells.push({ id: node.id, field, value });
    };
    let last = { r: R.r1, c: R.c1 };
    if (M.length === 1 && M[0].length === 1 && (R.r2 > R.r1 || R.c2 > R.c1)) {
      for (let r = R.r1; r <= R.r2; r++) for (let c = R.c1; c <= R.c2; c++) push(r, c, M[0][0]); // fill the range
      last = { r: R.r2, c: R.c2 };
    } else {
      M.forEach((row, i) =>
        row.forEach((value, j) => {
          const r = R.r1 + i;
          const c = R.c1 + j;
          if (r < n && c < cs.length) {
            push(r, c, value);
            last = { r: Math.max(last.r, r), c: Math.max(last.c, c) };
          }
        })
      );
    }
    if (!cells.length) return;
    S.ranges[key] = { a: { r: R.r1, c: R.c1 }, b: last };
    refresh(key);
    emitEvent('pcs_bulk', { table: key, kind: 'paste', cells });
  };

  S.selectAll = (key) => {
    key = key || S.active;
    const api = key && apiOf(key);
    if (!api || !nRows(api)) return;
    S.active = key;
    S.ranges[key] = { a: { r: 0, c: 0 }, b: { r: nRows(api) - 1, c: cols(api).length - 1 } };
    refresh(key);
  };

  S.pickFile = () => {
    if (S.uploadId != null) runMethod(S.uploadId, 'pickFiles', []);
  };

  /** Cell renderer of the trash column: a trash icon (shown in Edit mode, see pcs.css) that deletes its row. */
  S.trashCell = (p) => {
    const el = document.createElement('span');
    el.className = 'pcs-trash material-icons';
    el.textContent = 'delete';
    el.title = 'Delete this row (Edit mode; Ctrl+Z brings it back)';
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (p.data) S.deleteRow(p.context.key, p.data.id);
    });
    return el;
  };

  S.deleteRow = (key, id) => {
    if (readOnlyHint('delete rows')) return;
    const now = Date.now();
    if (now - S.lastDelete < 500) return; // a double click must not take the row that slides up under the pointer, too
    S.lastDelete = now;
    S.active = key;
    emitEvent('pcs_delete_row', { table: key, id });
  };

  function extend(key, k) {
    const api = apiOf(key);
    const g = ensureRange(key);
    if (!api || !g) return;
    const nr = nRows(api);
    const nc = cols(api).length;
    if (k === 'ArrowUp') g.b.r = Math.max(0, g.b.r - 1);
    else if (k === 'ArrowDown') g.b.r = Math.min(nr - 1, g.b.r + 1);
    else if (k === 'ArrowLeft') g.b.c = Math.max(0, g.b.c - 1);
    else if (k === 'ArrowRight') g.b.c = Math.min(nc - 1, g.b.c + 1);
    else if (k === 'Home') g.b.c = 0;
    else if (k === 'End') g.b.c = nc - 1;
    refresh(key);
    api.ensureIndexVisible(g.b.r);
    api.ensureColumnVisible(cols(api)[g.b.c]);
  }

  // ---------------------------------------------------------------- server -> client
  /** list = [[table, rowId, field, value], ...]  - updates the rows through rowNode.setData (re-evaluates row styles,
   *  keeps focus, fires no cellValueChanged echo). */
  S.apply = (list) => {
    const perGrid = {};
    for (const [table, id, field, value] of list) {
      const rows = (perGrid[table] = perGrid[table] || {});
      (rows[id] = rows[id] || {})[field] = value;
    }
    for (const key of Object.keys(perGrid)) {
      const api = apiOf(key);
      if (!api) continue;
      let heights = false;
      for (const [id, changes] of Object.entries(perGrid[key])) {
        const node = api.getRowNode(id);
        if (!node || !node.data) continue;
        node.setData({ ...node.data, ...changes });
        if (S.picker && S.picker.table === key && S.picker.rowId === id && '_flow' in changes) S.picker.refreshFrom(node.data);
        heights = heights || 'bins' in changes || 'comment' in changes;
      }
      if (heights) api.resetRowHeights();
    }
  };

  S.reset = () => {
    S.ranges = {};
    S.active = null;
  };

  /** Add row button: ask the server for a new row below the selected one (bottom row of the selection). */
  S.addRow = (key) => {
    const api = apiOf(key);
    if (!api) return;
    const R = currentRect(key);
    const node = R && api.getDisplayedRowAtIndex(R.r2);
    S.active = key;
    emitEvent('pcs_add_row', { table: key, after: node ? node.id : null });
  };

  /** ops = [{op: 'add', table, row, index, col}, {op: 'remove', table, id}] - rows the server added or took out
   *  (Add row, Delete, and Undo / Redo of them).  An added row with `col` gets the cursor there, in edit mode. */
  S.rows = (ops) => {
    const keys = new Set();
    for (const op of ops) {
      const api = apiOf(op.table);
      if (!api) continue;
      keys.add(op.table);
      if (op.op === 'remove') {
        const node = api.getRowNode(op.id);
        if (node) api.applyTransaction({ remove: [node.data] });
      } else {
        api.applyTransaction({ add: [op.row], addIndex: op.index });
      }
    }
    for (const key of keys) {
      delete S.ranges[key]; // displayed row numbers just moved
      refresh(key);
    }
    const added = ops.filter((o) => o.op === 'add').pop();
    const api = added && apiOf(added.table);
    const node = api && api.getRowNode(added.row.id);
    if (!node) return;
    api.ensureNodeVisible(node, 'middle');
    if (!added.col) return;
    S.active = added.table;
    api.setFocusedCell(node.rowIndex, added.col, undefined, true);
    const c = colIndex(api, added.col);
    if (c >= 0) S.ranges[added.table] = { a: { r: node.rowIndex, c }, b: { r: node.rowIndex, c } };
    if (S.editMode) setTimeout(() => api.startEditingCell({ rowIndex: node.rowIndex, colKey: added.col }), 80);
  };

  S.setEditMode = (on) => {
    S.editMode = !!on;
    document.body.classList.toggle('pcs-edit-mode', S.editMode);
    for (const key of allKeys()) {
      const api = apiOf(key);
      if (!api) continue;
      if (!S.editMode) api.stopEditing(true);
      refresh(key);
    }
  };

  S.setDirty = (dirty) => {
    window.onbeforeunload = dirty
      ? (e) => {
          e.preventDefault();
          e.returnValue = '';
          return '';
        }
      : null;
  };

  // ---------------------------------------------------------------- the picker: cell editor of Action / Alarm Config Key
  const NAV_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];

  const h = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  /** AG Grid cell editor of the Action and Alarm Config Key columns of the rule grid: a popup under the cell.
   *  Top: a search box and the choices (click one to add it - the same one may be added again).  Below: what is picked,
   *  in order - drag a row to move it, x takes it out.  What the list means, per column and product:
   *    Action (HST)             the actions of the rule; each one remembers the alarm it was, so it keeps its key
   *    Alarm Config Key (SST)   the keys, one ConfigKey alarm for each
   *    Alarm Config Key (HST)   one slot per ConfigKey action of the rule, in the order of those actions (x empties a slot)
   *  Closing the popup (Apply, Enter in an empty search box, a click outside, Tab) sends the list as pcs_flow when it
   *  changed; Esc and Cancel drop it.  The server writes the cell (pcs.apply), so getValue() hands the grid the old value. */
  class PickerEditor {
    init(params) {
      this.params = params;
      this.table = params.context && params.context.key;
      this.rowId = params.data && params.data.id;
      this.field = params.colDef.field;
      this.query = '';
      this.shown = []; // the choices the search text lets through
      this.hot = 0; // the highlighted one of them while there is a search text (Enter adds it)
      this.dragFrom = null;
      this.dropAt = null;
      this.done = false;
      this.cancelled = false;
      this.byAction = this.field === 'action';
      this.slots = !this.byAction && !isSst(); // HST keys: an empty slot is an item without a value
      this.title = this.byAction ? 'Action' : 'Alarm Config Key';
      this.choices = this.byAction ? S.actions.slice() : S.workflowKeys();
      this.load(params.data);
      // Esc closes the popup the way a click outside does - which would apply the list, so Esc is noted first
      this.onEsc = (e) => {
        if (e.key === 'Escape') this.cancelled = true;
      };
      document.addEventListener('keydown', this.onEsc, true);
      S.picking = true;
      S.picker = this;
      this.build();
      const typed = params.eventKey;
      if (typeof typed === 'string' && typed.length === 1) {
        this.search.value = typed; // the key that started the edit is the first letter to look for
        this.query = typed.toLowerCase();
        this.render();
      }
    }

    /** the list as the rule has it now: ``data`` is the row as the server sent it */
    load(data) {
      this.base = flowOf(data).map((s) => [String(s[0] || ''), String(s[1] || '')]); // (what the server checks the pick against)
      if (this.byAction) {
        this.items = this.base.map((s, i) => ({ v: s[0], from: i }));
      } else {
        const keys = (this.slots ? this.base.filter((s) => s[0] === CONFIG_KEY) : this.base).map((s) => s[1]);
        this.items = keys.filter((k) => this.slots || k).map((v) => ({ v }));
      }
      this.start = JSON.stringify(this.result());
    }

    /** The server sent new values for the row of this picker: the pick before it, applied just now (Tab goes from the
     *  Action to the Alarm Config Key cell at once, so that one was built from the rule as it was).  Start over from them
     *  while nothing was changed here; a list that was changed is refused by the server if it does not fit any more. */
    refreshFrom(data) {
      if (this.done || JSON.stringify(this.result()) !== this.start) return;
      this.load(data);
      this.render();
    }

    getGui() {
      return this.root;
    }

    afterGuiAttached() {
      this.search.focus();
      const end = this.search.value.length;
      this.search.setSelectionRange(end, end);
    }

    getValue() {
      return this.params.value;
    }

    /** The grid asks this once, when editing ends the normal way (not on Esc): the place to send the list. */
    isCancelAfterEnd() {
      this.commit();
      return true; // the grid must not write a value of its own
    }

    destroy() {
      document.removeEventListener('keydown', this.onEsc, true);
      if (S.picker === this) {
        S.picker = null;
        S.picking = false; // (not when the picker of the next cell has taken over already: Tab goes from one to the other)
      }
    }

    /** What goes to the server: Action - [action, place the alarm had or null] pairs; keys - the key of each item. */
    result() {
      return this.byAction ? this.items.map((x) => [x.v, x.from == null ? null : x.from]) : this.items.map((x) => x.v);
    }

    commit() {
      if (this.done) return;
      this.done = true;
      const result = this.result();
      if (this.cancelled || JSON.stringify(result) === this.start) return;
      emitEvent('pcs_flow', { table: this.table, id: this.rowId, field: this.field, items: result, base: this.base });
    }

    build() {
      const root = (this.root = h('div', 'pcs-pick'));
      const head = h('div', 'pcs-pick-head');
      head.append(h('span', 'pcs-pick-title', this.title), (this.note = h('span', 'pcs-pick-note')));
      const search = (this.search = h('input', 'pcs-pick-search'));
      search.type = 'text';
      search.placeholder = 'Search ...';
      search.autocomplete = 'off';
      search.spellcheck = false;
      search.addEventListener('input', () => {
        this.query = search.value.trim().toLowerCase();
        this.hot = 0;
        this.renderChoices();
      });
      search.addEventListener('keydown', (e) => this.onSearchKey(e));
      this.choiceBox = h('div', 'pcs-pick-list pcs-pick-choices');
      this.orderBox = h('div', 'pcs-pick-list pcs-pick-order');
      this.orderBox.addEventListener('dragover', (e) => {
        if (this.dragFrom == null || e.target !== this.orderBox) return; // (the rows have their own: before / after them)
        e.preventDefault();
        this.dropAt = this.items.length; // below the last row: put it last
        this.markDrop();
      });
      this.orderBox.addEventListener('drop', (e) => {
        e.preventDefault();
        this.finishDrag(true);
      });
      const foot = h('div', 'pcs-pick-foot');
      const cancel = h('button', 'pcs-pick-btn', 'Cancel');
      const apply = h('button', 'pcs-pick-btn pcs-pick-apply', 'Apply');
      cancel.type = apply.type = 'button';
      cancel.addEventListener('click', () => {
        this.cancelled = true;
        this.params.api.stopEditing(true);
      });
      apply.addEventListener('click', () => this.params.stopEditing());
      foot.append(cancel, apply);
      root.append(
        head,
        h('div', 'pcs-pick-sub', 'Choose'),
        search,
        this.choiceBox,
        h('div', 'pcs-pick-sub', 'Order - drag a row to move it'),
        h('div', 'pcs-pick-hint', this.hint()),
        this.orderBox,
        foot
      );
      root.addEventListener('keydown', (e) => {
        if (NAV_KEYS.includes(e.key)) e.stopPropagation(); // the arrows move inside the picker, not from cell to cell
      });
      this.render();
    }

    /** what the list means, in a line under its heading */
    hint() {
      if (this.slots) return 'One key for each ConfigKey action of the rule, in the order of those actions';
      if (this.byAction) return 'The alarms of the rule, in this order - a ConfigKey action keeps its key';
      return 'One ConfigKey alarm for each key, in this order';
    }

    render() {
      this.renderChoices();
      this.renderItems();
      const n = this.items.length;
      this.note.textContent = this.slots
        ? `${this.items.filter((x) => x.v).length} of ${n} key(s)`
        : this.byAction
        ? `${n} action(s)`
        : `${n} key(s)`;
    }

    /** every slot has its key: nothing more can be added (HST keys) */
    full() {
      return this.slots && this.items.every((x) => x.v);
    }

    renderChoices() {
      const box = this.choiceBox;
      box.textContent = '';
      const counts = new Map();
      for (const item of this.items) counts.set(item.v, (counts.get(item.v) || 0) + 1);
      this.shown = this.choices.filter((c) => !this.query || c.toLowerCase().includes(this.query));
      this.hot = Math.min(this.hot, Math.max(0, this.shown.length - 1));
      const full = this.full();
      this.shown.forEach((choice, i) => {
        const row = h('div', 'pcs-pick-opt');
        if (full) row.classList.add('pcs-off');
        if (this.query && i === this.hot) row.classList.add('pcs-hot');
        row.append(h('span', 'pcs-pick-name', choice));
        if (counts.has(choice)) row.append(h('span', 'pcs-pick-count', `\u00d7${counts.get(choice)}`));
        row.addEventListener('click', () => this.add(choice));
        box.append(row);
      });
      if (!this.shown.length) {
        let why = 'No match';
        if (!this.choices.length) {
          why = this.byAction ? 'No actions - AlarmIdMapping.xml was not found' : 'The List Workflow table has no keys';
        }
        box.append(h('div', 'pcs-pick-none', why));
      }
    }

    renderItems() {
      const box = this.orderBox;
      box.textContent = '';
      const known = new Set(this.choices);
      this.items.forEach((item, i) => {
        const row = h('div', 'pcs-pick-item');
        row.draggable = true;
        const name = h('span', 'pcs-pick-name', item.v || (this.byAction ? '(no action)' : '(no key yet)'));
        if (!item.v) {
          name.classList.add('pcs-pick-empty');
        } else if (this.choices.length && !known.has(item.v)) {
          row.classList.add('pcs-pick-unknown'); // the file has it, the list of choices does not: kept until taken out
          row.title = this.byAction ? 'Not in AlarmIdMapping.xml' : 'Not in the List Workflow table';
        }
        const remove = h('span', 'pcs-pick-x material-icons', 'close');
        remove.title = this.slots ? 'Empty this slot' : 'Take it out';
        remove.addEventListener('click', (e) => {
          e.stopPropagation();
          this.remove(i);
        });
        row.append(h('span', 'pcs-pick-grip material-icons', 'drag_indicator'), h('span', 'pcs-pick-num', `${i + 1}.`), name, remove);
        this.wireDrag(row, i);
        box.append(row);
      });
      if (!this.items.length) box.append(h('div', 'pcs-pick-none', 'Nothing picked - click a choice above'));
    }

    add(value) {
      if (this.slots) {
        const slot = this.items.findIndex((x) => !x.v);
        if (slot < 0) return;
        this.items[slot].v = value;
      } else {
        this.items.push(this.byAction ? { v: value, from: null } : { v: value });
      }
      this.render();
      this.search.focus();
    }

    remove(index) {
      if (this.slots) this.items[index].v = '';
      else this.items.splice(index, 1);
      this.render();
    }

    /** put the item at ``from`` in front of the one at ``to`` (``to`` counts the list as it is before the move) */
    move(from, to) {
      if (to === from || to === from + 1) return;
      const [item] = this.items.splice(from, 1);
      this.items.splice(to > from ? to - 1 : to, 0, item);
      this.render();
    }

    wireDrag(row, index) {
      row.addEventListener('dragstart', (e) => {
        this.dragFrom = index;
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(index));
        setTimeout(() => row.classList.add('pcs-dragging'), 0);
      });
      row.addEventListener('dragover', (e) => {
        if (this.dragFrom == null) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        const box = row.getBoundingClientRect();
        this.dropAt = index + (e.clientY > box.top + box.height / 2 ? 1 : 0);
        this.markDrop();
      });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        this.finishDrag(true);
      });
      row.addEventListener('dragend', () => this.finishDrag(false));
    }

    /** a line where the dragged row would land */
    markDrop() {
      const rows = this.orderBox.children;
      for (const row of rows) row.classList.remove('pcs-drop-before', 'pcs-drop-after');
      if (this.dropAt == null || !rows.length) return;
      if (this.dropAt < this.items.length) rows[this.dropAt].classList.add('pcs-drop-before');
      else rows[this.items.length - 1].classList.add('pcs-drop-after');
    }

    finishDrag(dropped) {
      const from = this.dragFrom;
      const to = this.dropAt;
      this.dragFrom = this.dropAt = null;
      if (dropped && from != null && to != null) this.move(from, to);
      else this.renderItems();
    }

    onSearchKey(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        e.stopPropagation(); // (not the grid's: Enter there would end the edit and move down)
        if (!this.query) {
          this.params.stopEditing(); // nothing to look for: Enter is "done"
          return;
        }
        const choice = this.shown[this.hot];
        if (choice !== undefined) this.add(choice);
        this.search.value = '';
        this.query = '';
        this.hot = 0;
        this.renderChoices();
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        e.stopPropagation();
        const n = this.shown.length;
        if (!n) return;
        this.hot = (this.hot + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
        this.renderChoices();
        const hot = this.choiceBox.querySelector('.pcs-hot');
        if (hot) hot.scrollIntoView({ block: 'nearest' });
      }
    }
  }
  S.PickerEditor = PickerEditor;

  // ---------------------------------------------------------------- DOM listeners (capture phase)
  document.addEventListener(
    'mousedown',
    (e) => {
      if (e.button !== 0) return;
      const key = keyOf(e.target);
      if (!key) return;
      const api = apiOf(key);
      const cell = api && cellAt(e.target, api);
      S.active = key;
      if (!cell) return;
      const g = S.ranges[key];
      if (e.shiftKey && g) g.b = cell;
      else S.ranges[key] = { a: { ...cell }, b: { ...cell } }; // two objects: Shift+arrows move only b
      S.shiftClick = e.shiftKey;
      S.dragging = key;
      refresh(key);
    },
    true
  );

  document.addEventListener(
    'mouseover',
    (e) => {
      if (!S.dragging || (e.buttons & 1) === 0) return;
      const key = keyOf(e.target);
      if (key !== S.dragging) return;
      const api = apiOf(key);
      const cell = api && cellAt(e.target, api);
      const g = S.ranges[key];
      if (!cell || !g || (g.b.r === cell.r && g.b.c === cell.c)) return;
      g.b = cell;
      refresh(key);
    },
    true
  );

  document.addEventListener(
    'mouseup',
    () => {
      S.dragging = null;
      setTimeout(() => (S.shiftClick = false), 0);
    },
    true
  );

  // Keyboard navigation / tabbing moves the focus: collapse the range onto the newly focused cell.
  document.addEventListener(
    'focusin',
    (e) => {
      const key = keyOf(e.target);
      if (!key) return;
      S.active = key;
      if (S.muteFocus || S.dragging || S.shiftClick) return;
      const api = apiOf(key);
      const cell = api && cellAt(e.target, api);
      if (!cell) return;
      const R = rectOf(S.ranges[key]);
      if (R && R.r1 === R.r2 && R.c1 === R.c2 && R.r1 === cell.r && R.c1 === cell.c) return;
      S.ranges[key] = { a: { ...cell }, b: { ...cell } };
      refresh(key);
    },
    true
  );

  document.addEventListener(
    'keydown',
    (e) => {
      const k = e.key;
      const m = e.ctrlKey || e.metaKey;
      const lk = k.length === 1 ? k.toLowerCase() : k;

      // Application-wide file shortcuts (also while typing).
      if (m && !e.altKey && !e.shiftKey && lk === 's') {
        stop(e);
        emitEvent('pcs_export');
        return;
      }
      if (m && !e.altKey && !e.shiftKey && lk === 'o') {
        stop(e);
        S.pickFile();
        return;
      }

      const t = e.target;
      if (isTextInput(t) || S.picking) return; // native Ctrl+C / V / X / Z in cell editors, dialogs, text boxes - and a picker keeps its keys
      const key = keyOf(t);

      if (m && !e.altKey) {
        if (lk === 'z') {
          stop(e);
          emitEvent(e.shiftKey ? 'pcs_redo' : 'pcs_undo');
        } else if (lk === 'y') {
          stop(e);
          emitEvent('pcs_redo');
        } else if (key && lk === 'c') {
          stop(e);
          S.active = key;
          S.copy(false);
        } else if (key && lk === 'x') {
          stop(e);
          S.active = key;
          S.copy(true);
        } else if (key && lk === 'a') {
          stop(e);
          S.selectAll(key);
        } else if (key && lk === 'v') {
          // Let the browser fire its paste event (no permission prompt); fall back to the async clipboard API.
          S.pasteSeen = false;
          clearTimeout(S.pasteTimer);
          S.pasteTimer = setTimeout(() => {
            if (!S.pasteSeen && navigator.clipboard && navigator.clipboard.readText) {
              navigator.clipboard.readText().then((txt) => S.paste(key, txt)).catch(() => {});
            }
          }, 120);
        }
        return;
      }

      if (!key) return;
      if (k === 'Delete' || k === 'Backspace') {
        stop(e);
        S.active = key;
        S.clear(key);
      } else if (e.shiftKey && ARROWS.includes(k)) {
        stop(e);
        extend(key, k);
      } else if (k === 'Escape') {
        const R = currentRect(key);
        if (R) {
          S.ranges[key] = { a: { r: R.r1, c: R.c1 }, b: { r: R.r1, c: R.c1 } };
          refresh(key);
        }
      }
    },
    true
  );

  document.addEventListener(
    'paste',
    (e) => {
      if (isTextInput(e.target) || S.picking) return;
      const key = keyOf(e.target) || S.active;
      if (!key) return;
      const text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
      stop(e);
      S.pasteSeen = true;
      clearTimeout(S.pasteTimer);
      S.active = key;
      S.paste(key, text);
    },
    true
  );
})();
