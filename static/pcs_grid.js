/* PCS Die File Checker - keyboard / clipboard layer for NiceGUI's AG Grid (Community edition).
 *
 * The server owns the data.  This file only
 *   1. tracks a rectangular cell range per grid (click, drag, Shift+click, Shift+arrows, Ctrl+A),
 *   2. turns Ctrl+C / Ctrl+X / Ctrl+V / Delete / Ctrl+Z / Ctrl+Y / Ctrl+S / Ctrl+O into events for the server,
 *   3. writes cell updates that the server sends back (pcs.apply) and shows rows it added / took out (pcs.rows),
 *   4. draws the trash icon of every row (pcs.trashCell) and asks the server to delete that row.
 * Real text inputs (cell editors, dialogs, the file-path box) keep the browser's native shortcuts.
 */
(() => {
  'use strict';

  const TRASH = '_del'; // id of the trash-icon column: not a data column, never part of a selection / copy
  const READ_ONLY = ['error', 'note', 'check']; // computed by the validation / the workers, never editable

  const S = (window.pcs = {
    editMode: false,
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
        if (READ_ONLY.includes(field)) continue;
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
    S.editMode && !READ_ONLY.includes(p.colDef.field) && !(((p.data || {})._ro) || []).includes(p.colDef.field);

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
      if (node && !READ_ONLY.includes(field)) cells.push({ id: node.id, field, value });
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
      if (isTextInput(t)) return; // native Ctrl+C / V / X / Z in cell editors, dialogs, text boxes
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
      if (isTextInput(e.target)) return;
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
