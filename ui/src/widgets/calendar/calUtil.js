// calendar: pure helpers (no React, no DOM). Contract: docs/fable/CALENDAR_API.md.
// Days are calendar days in Eastern time, carried as 'YYYY-MM-DD' strings; all day arithmetic is done
// on UTC midnights, so the browser's own time zone never shifts a date.

export const TYPE_ORDER = ['earnings', 'fed', 'econ', 'congress', 'court', 'fda', 'market', 'conference', 'company', 'ipo', 'expiry', 'personal'];
// What the chips offer: the server's twelve types plus one the widget makes itself. The API files the
// reminders Mason sets for its own follow-ups under "personal" with nothing to tell them from the
// owner's; they are recognised here (see masonSet) so "Mine" means the owner's own.
export const MASON_TYPE = 'mason';
export const VIEW_TYPES = [...TYPE_ORDER, MASON_TYPE];
/** Filter-chip labels when /api/calendar/meta is not available (same words as the server's). */
export const TYPE_LABELS = {
  earnings: 'Earnings',
  fed: 'Federal Reserve',
  econ: 'Economic data',
  congress: 'Congress',
  court: 'Courts',
  fda: 'FDA and trials',
  market: 'Market schedule',
  conference: 'Conferences',
  company: 'Company events',
  ipo: 'IPOs',
  expiry: 'Expiry and deadlines',
  personal: 'Mine',
  mason: 'Mason’s follow-ups'
};
/** The short label on one row. */
const ROW_LABELS = {
  earnings: 'Earnings',
  fed: 'Fed',
  econ: 'Economic data',
  congress: 'Congress',
  court: 'Court',
  fda: 'FDA',
  market: 'Market',
  conference: 'Conference',
  company: 'Company event',
  ipo: 'IPO',
  expiry: 'Options expiry',
  personal: 'My events',
  mason: 'Mason’s follow-up'
};
// The "expiry" type also carries filing and tax deadlines and Mason's planned exits.
const KIND_LABELS = {
  deadline_13f: 'Deadline',
  tax_date: 'Deadline',
  deadline: 'Deadline',
  time_stop: 'Planned exit',
  trial_completion: 'Drug trial'
};
export const IMPORTANCE_TEXT = {
  1: 'Worth knowing',
  2: 'Can move a sector or one of your stocks',
  3: 'Can move the whole market, or it is yours'
};

export const RANGE_STEP = 60;
export const RANGE_MAX = 365;
export const MAX_ROWS = 500;
export const FILTER_KEY = 'mason.calendar.filters.v1';

const DAY_MS = 86400000;
const YMD_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;

const str = v => (typeof v === 'string' ? v : '');
export const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);

// ── days ─────────────────────────────────────────────────────────────────────
/** 'YYYY-MM-DD' → day number (days since 1970-01-01), or null for anything that is not a real date. */
export function dayNum(s) {
  const m = typeof s === 'string' ? s.match(YMD_RE) : null;
  if (!m) return null;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const d = new Date(t);
  if (d.getUTCFullYear() !== +m[1] || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3]) return null;
  return Math.round(t / DAY_MS);
}
export const isYmd = s => dayNum(s) != null;
export function ymd(n) {
  return new Date(n * DAY_MS).toISOString().slice(0, 10);
}
export function addDays(s, n) {
  const d = dayNum(s);
  return d == null ? null : ymd(d + n);
}
/** Whole days from a to b (b - a), or null. */
export function diffDays(a, b) {
  const x = dayNum(a);
  const y = dayNum(b);
  return x == null || y == null ? null : y - x;
}

/** Today's date in Eastern time, from the browser clock (the server's own `today` wins once loaded). */
export function etToday(now = new Date()) {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
    const get = k => parts.find(p => p.type === k).value;
    const s = `${get('year')}-${get('month')}-${get('day')}`;
    if (isYmd(s)) return s;
  } catch {
    /* no time-zone data: fall through to the local date */
  }
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

const fmtCache = new Map();
function fmt(opts) {
  const k = JSON.stringify(opts);
  if (!fmtCache.has(k)) fmtCache.set(k, new Intl.DateTimeFormat('en-US', { ...opts, timeZone: 'UTC' }));
  return fmtCache.get(k);
}
const asDate = s => new Date(dayNum(s) * DAY_MS);
const yearOf = s => (isYmd(s) ? +s.slice(0, 4) : null);

/** "Nov 17" (the year is added when it is not `today`'s year). */
export function shortDay(s, today) {
  if (!isYmd(s)) return '';
  const withYear = today && yearOf(s) !== yearOf(today);
  return fmt({ month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}) }).format(asDate(s));
}
/** "Tue, Nov 17" (plus the year when it is not `today`'s year). */
export function weekDay(s, today) {
  if (!isYmd(s)) return '';
  const withYear = today && yearOf(s) !== yearOf(today);
  return fmt({ weekday: 'short', month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}) }).format(asDate(s));
}
/** "Tuesday, November 17, 2026" */
export function longDay(s) {
  return isYmd(s) ? fmt({ weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }).format(asDate(s)) : '';
}
export const dowShort = s => (isYmd(s) ? fmt({ weekday: 'short' }).format(asDate(s)) : '');
export const dayOfMonth = s => (isYmd(s) ? String(+s.slice(8, 10)) : '');

/** "today", "tomorrow", "in 2 days", "in 5 weeks", "in about 4 months"; "yesterday", "3 days ago". */
export function relDay(s, today) {
  const n = diffDays(today, s);
  if (n == null) return '';
  if (n === 0) return 'today';
  if (n === 1) return 'tomorrow';
  if (n === -1) return 'yesterday';
  if (n < 0) return `${-n} days ago`;
  if (n <= 20) return `in ${n} days`;
  if (n <= 62) return `in ${Math.round(n / 7)} weeks`;
  return `in about ${Math.round(n / 30.44)} months`;
}
/** The sticky header of one day: "Today · Sun, Nov 15", "Tomorrow · Mon, Nov 16", "Tue, Nov 17 · in 2 days". */
export function dayHeader(s, today) {
  const n = diffDays(today, s);
  const day = weekDay(s, today);
  if (n === 0) return `Today · ${day}`;
  if (n === 1) return `Tomorrow · ${day}`;
  const rel = relDay(s, today);
  return rel ? `${day} · ${rel}` : day;
}

/** '14:30' → "2:30 pm"; anything that is not a clock time → ''. */
export function clock(hhmm) {
  const m = typeof hhmm === 'string' ? hhmm.match(/^([01]?\d|2[0-3]):([0-5]\d)$/) : null;
  if (!m) return '';
  const h = +m[1];
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? 'am' : 'pm'}`;
}

// ── events ───────────────────────────────────────────────────────────────────
const MASON_PREFIX = /^\[(?:Opus|Mason)\b[^\]]{0,40}\]\s*/;
/**
 * A reminder Mason set for itself. The API says who set each one (`created_by`: 'nico' or 'mason', sent
 * since 2026-11-16); a server from before that is read by the "[Opus follow-up]" prefix mason_director.py
 * puts on the reminders it writes.
 */
function masonSet(raw, id, title) {
  if (raw.type !== 'personal' || !id.startsWith('rem:')) return false;
  if (typeof raw.created_by === 'string' && raw.created_by) return raw.created_by.toLowerCase() !== 'nico';
  return MASON_PREFIX.test(title);
}

/** One API event → the fields the widget uses, every one type-checked. null when it has no id or day. */
export function cleanEvent(raw) {
  if (!isObj(raw)) return null;
  const id = typeof raw.id === 'string' || typeof raw.id === 'number' ? String(raw.id) : '';
  if (!id || !isYmd(raw.date)) return null;
  const rawTitle = str(raw.title).trim();
  const mason = masonSet(raw, id, rawTitle);
  const type = mason ? MASON_TYPE : TYPE_ORDER.includes(raw.type) ? raw.type : 'personal';
  const end = isYmd(raw.end_date) && dayNum(raw.end_date) > dayNum(raw.date) ? raw.end_date : null;
  const time = clock(raw.time_et) ? raw.time_et.padStart(5, '0') : null;
  // Mason's own follow-ups are not "yours": they never get the top mark
  const imp = raw.importance === 1 ? 1 : raw.importance === 3 && !mason ? 3 : 2;
  const tickers = Array.isArray(raw.tickers) ? raw.tickers.filter(t => typeof t === 'string' && t.trim()).map(t => t.trim().toUpperCase().slice(0, 12)) : [];
  const ch = isObj(raw.changed) && isYmd(raw.changed.previous_date) ? { previous_date: raw.changed.previous_date, changed_on: isYmd(raw.changed.changed_on) ? raw.changed.changed_on : null } : null;
  return {
    id,
    date: raw.date,
    end_date: end,
    time_et: time,
    type,
    kind: str(raw.kind),
    title: (mason ? rawTitle.replace(MASON_PREFIX, '') : rawTitle) || rawTitle || 'Untitled event',
    full_title: rawTitle || 'Untitled event',
    subtitle: str(raw.subtitle).trim(),
    tickers,
    held: raw.held === true,
    watched: raw.watched === true,
    importance: imp,
    confirmed: raw.confirmed !== false,
    source: str(raw.source).trim(),
    url: safeUrl(raw.url),
    changed: ch,
    done: raw.status === 'done',
    editable: raw.editable === true,
    note: str(raw.note).trim()
  };
}

/** The server's order: day, then time (all-day first), then the bigger event, then the title. */
export function sortEvents(list) {
  return list.slice().sort((a, b) => {
    if (a.date !== b.date) return a.date < b.date ? -1 : 1;
    const ta = a.time_et || '';
    const tb = b.time_et || '';
    if (ta !== tb) return ta < tb ? -1 : 1;
    if (a.importance !== b.importance) return b.importance - a.importance;
    const la = a.title.toLowerCase();
    const lb = b.title.toLowerCase();
    if (la !== lb) return la < lb ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

/** Several response bodies → one clean, sorted list with no id twice. */
export function mergeEvents(...lists) {
  const seen = new Map();
  for (const l of lists) {
    for (const raw of Array.isArray(l) ? l : []) {
      const ev = cleanEvent(raw);
      if (ev && !seen.has(ev.id)) seen.set(ev.id, ev);
    }
  }
  return sortEvents([...seen.values()]);
}

/** http / https only (never javascript:, data:, or a relative path). Returns the normalized URL or null. */
export function safeUrl(u) {
  if (typeof u !== 'string' || !u.trim() || u.length > 2000) return null;
  try {
    const p = new URL(u.trim());
    return p.protocol === 'http:' || p.protocol === 'https:' ? p.href : null;
  } catch {
    return null;
  }
}

export function rowLabel(ev) {
  if (ev.type === 'expiry' && ev.kind && !/opex|witching|option/.test(ev.kind)) return KIND_LABELS[ev.kind] || 'Deadline';
  return KIND_LABELS[ev.kind] || ROW_LABELS[ev.type] || 'Event';
}

/** The many small drug-trial dates: FDA items of the lowest importance. Hidden unless asked for. */
export const isMinorTrial = ev => ev.type === 'fda' && ev.importance < 2;

/** A whole calendar month with no confirmed day ("expected May 2027"). */
export function isMonthGuess(ev) {
  if (ev.confirmed || !ev.end_date) return false;
  if (ev.date.slice(8) !== '01' || ev.date.slice(0, 7) !== ev.end_date.slice(0, 7)) return false;
  return addDays(ev.end_date, 1).slice(8) === '01';
}

/** The words after the title for an event longer than a day: "through Nov 20", "any day in May 2027". */
export function rangeText(ev, today) {
  if (!ev.end_date) return '';
  if (isMonthGuess(ev)) return `any day in ${fmt({ month: 'long', year: 'numeric' }).format(asDate(ev.date))}`;
  const started = today && dayNum(ev.date) < dayNum(today);
  return `${started ? `started ${shortDay(ev.date, today)}, ` : ''}through ${shortDay(ev.end_date, today)}`;
}

/** The day an event is listed under: its first day, or today when it began earlier and is still on. */
export const listDay = (ev, today) => (today && dayNum(ev.date) < dayNum(today) ? today : ev.date);

/** The answer's `held_tickers` → a clean, sorted list of stock symbols; null when the server did not send one. */
export function cleanHeld(list) {
  if (!Array.isArray(list)) return null;
  const out = new Set();
  for (const t of list) {
    const s = typeof t === 'string' ? t.trim().toUpperCase() : '';
    if (TICKER_RE.test(s)) out.add(s);
  }
  return [...out].sort();
}

/**
 * The stocks Nico or Mason hold: the server's list when it sent one (`held`, from cleanHeld). A server from
 * before 2026-11-16 sends none; then any event with exactly one ticker that is marked held names it.
 */
export function heldSet(events, held = null) {
  if (Array.isArray(held)) return new Set(held);
  const out = new Set();
  for (const ev of events) if (ev.held && ev.tickers.length === 1) out.add(ev.tickers[0]);
  return out;
}

// ── filters ──────────────────────────────────────────────────────────────────
export const DEFAULT_FILTERS = { types: [], mine: false, trials: false };

export function cleanFilters(f) {
  const o = isObj(f) ? f : {};
  const types = Array.isArray(o.types) ? VIEW_TYPES.filter(t => o.types.includes(t)) : [];
  return { types: types.length === VIEW_TYPES.length ? [] : types, mine: o.mine === true, trials: o.trials === true };
}
export function loadFilters(storage) {
  try {
    return cleanFilters(JSON.parse(storage.getItem(FILTER_KEY) || 'null'));
  } catch {
    return { ...DEFAULT_FILTERS };
  }
}
export function saveFilters(storage, f) {
  try {
    storage.setItem(FILTER_KEY, JSON.stringify(cleanFilters(f)));
  } catch {
    /* private mode / storage full: the choice just is not remembered */
  }
}

/** Everything but the type chips (which also need the counts of the types that are switched off). */
export function applyBase(events, f) {
  return events.filter(ev => (!f.mine || ev.held) && (f.trials || !isMinorTrial(ev)));
}
export function applyTypes(events, f) {
  return f.types.length ? events.filter(ev => f.types.includes(ev.type)) : events;
}
export function typeCounts(events) {
  const out = Object.fromEntries(VIEW_TYPES.map(t => [t, 0]));
  for (const ev of events) out[ev.type] += 1;
  return out;
}
/** Chip click: from "all" pick just that type; otherwise switch it on or off; nothing left = all again. */
export function toggleType(types, t) {
  const next = types.includes(t) ? types.filter(x => x !== t) : [...types, t];
  return cleanFilters({ types: next }).types;
}

// ── views ────────────────────────────────────────────────────────────────────
/** → [{date, events}] in day order, each event listed once (on listDay). */
export function groupByDay(events, today) {
  const map = new Map();
  for (const ev of events) {
    const d = listDay(ev, today);
    if (!map.has(d)) map.set(d, []);
    map.get(d).push(ev);
  }
  return [...map.keys()].sort().map(date => ({ date, events: map.get(date) }));
}

/** Flat rows for the list: a day header row, then that day's events. Capped at `max` rows. */
export function agendaRows(groups, max = MAX_ROWS) {
  const rows = [];
  let total = 0;
  for (const g of groups) {
    total += g.events.length;
    if (rows.length >= max) continue;
    rows.push({ sep: true, key: `d:${g.date}`, date: g.date, count: g.events.length });
    for (const ev of g.events) {
      if (rows.length >= max) break;
      rows.push({ sep: false, key: `e:${ev.id}`, ev });
    }
  }
  const shown = rows.filter(r => !r.sep).length;
  return { rows, shown, total };
}

const SPAN_MARK_MAX = 14; // longer spans ("any day in May") do not mark every day they cover

// ── the month grid ───────────────────────────────────────────────────────────
/** 'YYYY-MM' of a day, or null. */
export const monthOf = s => (isYmd(s) ? s.slice(0, 7) : null);
/** 'YYYY-MM' + n months → 'YYYY-MM'. */
export function addMonths(ym, n) {
  const m = typeof ym === 'string' ? ym.match(/^(\d{4})-(\d{2})$/) : null;
  if (!m) return null;
  const i = +m[1] * 12 + (+m[2] - 1) + n;
  return `${String(Math.floor(i / 12)).padStart(4, '0')}-${String((i % 12) + 1).padStart(2, '0')}`;
}
/** Whole months from a to b ('YYYY-MM'). */
export const monthDiff = (a, b) => (+b.slice(0, 4) - +a.slice(0, 4)) * 12 + (+b.slice(5, 7) - +a.slice(5, 7));
/** "November 2026" */
export const monthTitle = ym => fmt({ month: 'long', year: 'numeric' }).format(asDate(`${ym}-01`));
/** "November" */
export const monthName = ym => fmt({ month: 'long' }).format(asDate(`${ym}-01`));
export const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/**
 * The boxes of one month, Sunday first: {month, first, last, from, to, days[]}. `first`/`last` are the
 * month's own first and last day; `from`/`to` the first and last box (the days before and after the
 * month that fill its first and last week).
 */
export function monthGrid(ym) {
  const first = `${ym}-01`;
  const f = dayNum(first);
  const last = ymd(dayNum(`${addMonths(ym, 1)}-01`) - 1);
  const start = f - asDate(first).getUTCDay();
  const end = dayNum(last) + (6 - asDate(last).getUTCDay());
  const days = [];
  for (let d = start; d <= end; d++) days.push(ymd(d));
  return { month: ym, first, last, from: days[0], to: days[days.length - 1], days };
}

/** True when the event is on at least one day from `from` to `to` (inclusive). */
export function overlaps(ev, from, to) {
  const a = dayNum(ev.date);
  const b = ev.end_date ? dayNum(ev.end_date) : a;
  return a <= dayNum(to) && Math.max(a, b) >= dayNum(from);
}
/** The box an event is shown in: its first day, or the first box when it began before the grid. */
export const placeDay = (ev, from) => (dayNum(ev.date) < dayNum(from) ? from : ev.date);

/**
 * One cell per box: {date, inMonth, events[], ongoing[], count, types[], held, big}. `events` start in that
 * box (or began before the grid and are still on); `ongoing` are short multi-day events carried on from an
 * earlier box. `count` and the dots are about `events` only.
 */
export function monthCells(events, grid) {
  const t0 = dayNum(grid.from);
  const n = grid.days.length;
  const cells = grid.days.map(date => ({ date, inMonth: date.slice(0, 7) === grid.month, events: [], ongoing: [], count: 0, types: [], held: false, big: false }));
  for (const ev of events) {
    if (!overlaps(ev, grid.from, grid.to)) continue;
    const at = dayNum(placeDay(ev, grid.from)) - t0;
    const c = cells[at];
    c.events.push(ev);
    c.count += 1;
    if (!c.types.includes(ev.type)) c.types.push(ev.type);
    if (ev.held) c.held = true;
    if (ev.importance === 3) c.big = true;
    if (ev.end_date && !isMonthGuess(ev) && dayNum(ev.end_date) - dayNum(ev.date) <= SPAN_MARK_MAX) {
      const lastBox = Math.min(dayNum(ev.end_date) - t0, n - 1);
      for (let i = at + 1; i <= lastBox; i++) cells[i].ongoing.push(ev);
    }
  }
  for (const c of cells) c.types.sort((a, b) => VIEW_TYPES.indexOf(a) - VIEW_TYPES.indexOf(b));
  return cells;
}

/** The events on during the month itself (not the dimmed boxes of the months around it). */
export const inMonth = (events, grid) => events.filter(ev => overlaps(ev, grid.first, grid.last));
/** The month's own events for the agenda: [{date, events}], an event that began earlier listed on the 1st. */
export const monthGroups = (events, grid) => groupByDay(inMonth(events, grid), grid.first);

// ── the add / edit form ──────────────────────────────────────────────────────
/** "nvda, smci tsm" → ['NVDA','SMCI','TSM'] or null when a piece is not a stock symbol. */
export function parseTickers(text) {
  const parts = String(text || '')
    .toUpperCase()
    .split(/[\s,;]+/)
    .filter(Boolean);
  const out = [];
  for (const p of parts) {
    if (!TICKER_RE.test(p)) return null;
    if (!out.includes(p)) out.push(p);
  }
  return out;
}

/**
 * fields = {title, date, end, time, tickers, note}. `end` = the last day of an event that lasts several days
 * (optional). `full` = false for a reminder, which only has a name and a day.
 * → {errors: {field: 'plain sentence'}, body} — body is what to send when there are no errors; body.end_date is
 * '' for a one-day event (a last day equal to the first day counts as one day).
 */
export function validateForm(fields, { today, full = true } = {}) {
  const errors = {};
  const title = String(fields.title || '').trim();
  if (!title) errors.title = 'Give the event a name.';
  else if (title.length > 200) errors.title = 'That name is too long. Keep it under 200 characters.';
  const date = String(fields.date || '').trim();
  if (!date) errors.date = 'Pick a date.';
  else if (!isYmd(date)) errors.date = 'That date does not look right. Use the date picker.';
  else if (today && dayNum(date) < dayNum(today)) errors.date = 'That date has passed. Pick today or a later day.';
  const body = { title, date };
  if (full) {
    const end = String(fields.end || '').trim();
    if (end && !isYmd(end)) errors.end = 'That last day does not look right. Use the date picker, or leave it empty.';
    else if (end && isYmd(date) && dayNum(end) < dayNum(date)) errors.end = 'The last day is before the first day. Pick a later day, or leave it empty.';
    body.end_date = end && isYmd(end) && isYmd(date) && dayNum(end) > dayNum(date) ? end : '';
    const time = String(fields.time || '').trim();
    if (time && !clock(time)) errors.time = 'That time does not look right. Use the time picker, or leave it empty.';
    const tickers = parseTickers(fields.tickers);
    if (tickers === null) errors.tickers = 'Stock symbols are short and start with a letter, like NVDA. Separate them with commas.';
    else if (tickers.length > 12) errors.tickers = 'That is a lot of stocks. Keep it to 12 or fewer.';
    const note = String(fields.note || '').trim();
    if (note.length > 500) errors.note = 'That note is too long. Keep it under 500 characters.';
    body.time = time;
    body.tickers = tickers || [];
    body.note = note;
  }
  return { errors, body };
}

/**
 * The form's first day changed to `date`: a last day already filled in moves with it, so the event keeps its
 * length (as the server does when only the first day is sent). Anything not a real day is left as it is.
 * (review 2026-11-16)
 */
export function moveFirstDay(fields, date) {
  const shift = diffDays(fields.date, date);
  const end = String(fields.end || '');
  const keep = shift != null && shift !== 0 && dayNum(end) != null && dayNum(end) > dayNum(fields.date);
  return { ...fields, date, end: keep ? addDays(end, shift) : fields.end };
}

/** The event asked about is gone (removed elsewhere). Since 2026-11-16 the server says so with code 'not_found'. */
export function isGone(status, body) {
  return status === 404 && isObj(body) && (body.code === 'not_found' || body.error === 'not found');
}

/**
 * A failed save, in plain words. `status` 0 = no answer at all. Since 2026-11-16 the server words its
 * refusals for Nico and tags them with a `code`: such a sentence is shown as it is (as text, at most 240
 * characters). An older server's short messages are translated below; a crash never shows its details.
 */
export function writeErrorText(status, body) {
  const e = isObj(body) && typeof body.error === 'string' ? body.error : '';
  const code = isObj(body) && typeof body.code === 'string' ? body.code : '';
  if (status >= 500 || code === 'server_error') return 'Mason could not save that just now. Try again in a minute.';
  if (isGone(status, body)) return 'That event is no longer on the calendar.'; // the server sends just "not found" (review)
  if (code && status >= 400 && status < 500 && e.trim()) return e.replace(/\s+/g, ' ').trim().slice(0, 240);
  if (status === 404 && e === 'not found') return 'That event is no longer on the calendar.';
  if (status === 404) return "The calendar turns on after Mason's next restart.";
  if (status === 400) {
    if (/title/i.test(e)) return 'Give the event a name.';
    if (/bad (end )?date/i.test(e)) return 'That date does not look right.';
    if (/bad time/i.test(e)) return 'That time does not look right.';
    if (/open position/i.test(e)) return 'This comes from an open position. Change the position, not the calendar entry.';
    if (/comes from|only events you added/i.test(e)) return 'This entry comes from an outside source, so it cannot be changed here.';
    return 'That could not be saved. Check what you typed and try again.';
  }
  return 'That could not be saved. Check the connection and try again.';
}
