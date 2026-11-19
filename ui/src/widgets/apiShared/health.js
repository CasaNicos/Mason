// API page view model (UI overhaul Task 6, 2026-11-16). Pure: no React, no DOM, no fetch.
//
// GET /api/health (mason_app.py api_health) returns, at the top level (not the RH envelope):
//   sources[]        mason_health.HealthMonitor.snapshot(): {name, kind: broker|llm|data|feed, ok: true|false|null,
//                    state: "up"|"down"|"limit"|"not_configured"|null, latency_ms, detail, note, last_checked (epoch s),
//                    last_checked_iso (ET), since (epoch s, start of the current state), since_iso (ET)}
//   generated_at     server epoch seconds (null when the snapshot raised)
//   detail           "health monitor not started" (monitor off)      error   snapshot exception text
//   background_jobs  mason_runtime.snapshot(): {ok, problems[], jobs[{name, label, status: ok|off|stale|dead|
//                    gave_up|restarting, every_seconds, stale_after_seconds, last_beat_age_seconds, beats,
//                    busy_with, busy_for_seconds, restarts_last_hour, last_error}], supervisor_running,
//                    watchdog{enabled, pings, last_ping}, reconcile{at, kind, ok, fills_seen, ...}, generated_at}
//   APIGPU 2026-11-17: sources[] may also hold the local model's graphics-card check (gpuhealth backend,
//                    /var/tmp/mason_stage_gpuhealth/CONTRACT.json): {name: "local_model_gpu", kind: "model", ok,
//                    state: "on_gpu"|"on_cpu"|"idle"|"gpu_missing"|"down" (null before its first check), detail (plain
//                    sentences, the advice included), latency_ms, model, vram_mb, size_mb (decimal MB), gpu_name, temp_c,
//                    power_w, power_limit_w, checked_at (ET ISO)} plus the usual note / last_checked / since fields.
//                    Every extra field may be null or missing. Until the backend sends it, nothing about it is shown
//                    (no node, no line, no legend entry).
// Every string from the server is rendered as JSX text (escaped). Nothing here builds HTML.

export const ROUTE = '/api/health';
/** Same cadence as the template's API-page poller (mason_v2.html: refreshHealth every 45 s on #api). */
export const POLL_MS = 45000;

// The four health states (summary tiles, kind-bar issues). 'unconfigured' is deliberately NOT in this list: an
// optional source with no key is neither healthy nor an issue, so it is counted on its own (STATES has all five).
export const STATE_ORDER = ['up', 'limit', 'down', 'unknown'];
// APIGPU: 'slow' = the local model answering from the processor instead of the graphics card (APIAMBER: or mostly from
// it). An issue (amber, listed under Issues, counted in the kind bars) but not a tile: it has its own summary line.
export const STATES = [...STATE_ORDER, 'unconfigured', 'slow'];
/** R3-C 2026-11-17: the calm hint shown wherever a not-configured source appears. */
export const NOT_CONFIGURED_HINT = 'Not configured: add a key to enable';

/** Status → text + glyph. The glyph is the non-color signal (never color alone). */
export const STATE_META = {
  up: { label: 'Up', short: 'UP', glyph: '▲', tone: 'up', pulse: 'data flowing' },
  limit: {
    label: 'Limit hit',
    short: 'LIMIT HIT',
    glyph: '◆',
    tone: 'warn',
    pulse: 'throttled: quota / rate limit',
    title: 'API quota / rate limit hit — reachable but capped, not an outage'
  },
  down: { label: 'Down', short: 'DOWN', glyph: '▼', tone: 'down', pulse: 'no data flowing' },
  unknown: { label: 'Not checked', short: 'N/A', glyph: '○', tone: 'idle', pulse: 'awaiting first check' },
  // R3-C: mason_health state "not_configured" (probe ok None, e.g. congress_gov with no key). Hollow dotted
  // glyph, the faintest tone, no pulses: intentional and calm, never an outage, distinct from "not checked".
  unconfigured: {
    label: 'Not configured',
    short: 'NOT SET', // chip text (rows, tooltip); the long label + hint say "Not configured"
    glyph: '◌',
    tone: 'off',
    pulse: 'no link: optional source, not set up',
    title: 'Optional source with no key or setup yet. Nothing is broken; add a key to enable it.'
  },
  // APIGPU: generic words for the 'slow' bucket (kind bars, legend). The local model's own words are in MODEL_META.
  slow: {
    label: 'Slow',
    short: 'SLOW',
    glyph: '◆',
    tone: 'warn',
    pulse: 'crawling: about 10× slower',
    title: 'Working, but much slower than it should be'
  }
};

// ── APIGPU (2026-11-17): the local AI model on the graphics card ────────────────────────────────────────────
// The gpuhealth check (/api/health source "local_model_gpu", kind "model") says WHERE qwen runs. Each of its
// states maps onto the page's health buckets (state: what it counts as) and gets its own look and plain words:
//   on_gpu      → up    (the normal healthy style)            ▲ ON GPU   "On the graphics card"
//     … but less than half of the model on the card
//               → slow  (an issue: amber; APIAMBER below)     ◆ MOSTLY CPU  "Mostly on the processor"
//   idle        → up    (nothing wrong: calm, look "asleep")  ☾ ASLEEP   "Asleep" (wakes up on the next request)
//   on_cpu      → slow  (an issue: amber, ~10× slower)        ◆ ON CPU   "On the processor"
//   gpu_missing → down  (an error)                            ▼ NO GPU   "Graphics card not seen"
//   down        → down  (an error: Ollama isn't answering)    ▼ DOWN     "Down"
// The glyph and the words are the non-color signal. An unknown state falls back to the generic rule (ok → up/down).
// REVIEW 2026-11-17: every word here must be true in EVERY backend case of its state (CONTRACT.json examples):
// on_gpu may be only partly on the card (modelHint says so), and gpu_missing also covers "status tool stuck" and
// "nvidia-smi not installed", where Ollama still reports the model on the card: so "can't see", not "missing".
// APIAMBER (Nico, 2026-11-18, decision 3 in docs/fable/API_GPU_FOR_NICO.md): on_gpu with LESS THAN HALF of the model
// on the card is shown amber like on_cpu (an issue, amber node and pulses, a headline note). The backend still sends
// on_gpu / ok true there, so the page makes this one state itself ("mostly_cpu") from the two numbers, with the
// backend's own split (mostlyOnCpu below). The name is never taken from the server. Half or more stays on_gpu (green).
export const MODEL_SOURCE = 'local_model_gpu';
export const MODEL_KIND = 'model';
/** At or above this card temperature the reading is marked "hot": the gpuhealth backend's own "running hot"
 *  threshold (it adds a sentence to `detail` after 2 minutes at 83 °C or more; the page marks the reading itself). */
export const HOT_C = 83;
export const MODEL_META = {
  on_gpu: {
    state: 'up',
    look: 'up',
    tone: 'up',
    glyph: '▲',
    label: 'On the graphics card',
    short: 'ON GPU',
    inState: 'On the card for',
    hint: 'Running on the graphics card, at full speed.', // only when all of it is on the card: see modelHint()
    pulse: 'running on the graphics card', // (not shown today: an up link's pulse line gives its speed)
    title: 'The local AI model is running on the graphics card.'
  },
  idle: {
    state: 'up',
    look: 'asleep',
    tone: 'calm',
    glyph: '☾',
    label: 'Asleep',
    short: 'ASLEEP',
    inState: 'Asleep for',
    hint: "Asleep: nothing's loaded right now. It wakes up on its own with the next request.",
    pulse: 'resting: wakes up on the next request',
    title: "Nothing's loaded right now. That's normal between jobs: the model wakes up on its own when Mason needs it."
  },
  on_cpu: {
    state: 'slow',
    look: 'slow',
    tone: 'warn',
    glyph: '◆',
    label: 'On the processor',
    short: 'ON CPU',
    inState: 'On the processor for',
    hint: "Running on the processor instead of the graphics card, so it's about 10× slower.",
    note: 'on the processor, about 10× slower',
    lead: 'On the processor, about 10× slower than on the graphics card', // the note under "Local AI running slow"
    legend: 'the local AI model is on the processor (about 10× slower)', // the constellation legend's hover text
    pulse: 'crawling: about 10× slower than on the graphics card',
    title: 'The local AI model is answering from the processor, not the graphics card: about 10 times slower.'
  },
  gpu_missing: {
    state: 'down',
    look: 'down',
    tone: 'down',
    glyph: '▼',
    label: 'Graphics card not seen',
    short: 'NO GPU',
    inState: 'Not seen for',
    hint: "Mason can't see the graphics card right now. If it has dropped out, the model runs on the processor instead, about 10× slower.",
    note: "Mason can't see the graphics card",
    pulse: 'graphics card not seen',
    title: "Mason can't see the graphics card right now."
  },
  down: {
    state: 'down',
    look: 'down',
    tone: 'down',
    glyph: '▼',
    label: 'Down',
    short: 'DOWN',
    inState: 'Down for',
    hint: "Ollama (the program that runs the local model) isn't answering, so the model can't run right now.",
    note: "Ollama isn't answering",
    pulse: 'no data flowing',
    title: "Ollama, the program that runs the local AI model, isn't answering."
  },
  // APIAMBER: made by the page, never a backend state: on_gpu with less than half of the model on the card.
  // `splitFrom` = the backend state it is split from. The server's `since` is when THAT state began (the model
  // coming onto the card), not when it went under half: so the time words say "loaded for", the card's "Last OK" is
  // unknown.
  mostly_cpu: {
    splitFrom: 'on_gpu',
    state: 'slow',
    look: 'slow',
    tone: 'warn',
    glyph: '◆',
    label: 'Mostly on the processor',
    short: 'MOSTLY CPU',
    inState: 'Loaded for',
    forText: 'loaded', // the source list's "… for 25m" (every other state uses its label there)
    hint: "Running mostly on the processor: less than half of it fits on the graphics card, so it's much slower than usual.",
    note: 'mostly on the processor, much slower than usual',
    lead: 'Mostly on the processor: less than half of it fits on the graphics card',
    legend: 'the local AI model is mostly on the processor (much slower than usual)',
    pulse: 'crawling: mostly on the processor, much slower than usual',
    title: 'The local AI model is running mostly on the processor: less than half of it fits on the graphics card.'
  }
};
/** The five states the gpuhealth backend sends (CONTRACT.json). Only these are read from the server's `state`. */
export const BACKEND_MODEL_STATES = Object.keys(MODEL_META).filter(k => !MODEL_META[k].splitFrom);
const hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

/** True for the local-model source (raw or normalized): kind "model", or the gpuhealth name. */
export function isModelSource(s) {
  if (!s || typeof s !== 'object') return false;
  return String(s.kind || '').toLowerCase() === MODEL_KIND || s.name === MODEL_SOURCE;
}

export const JOB_META = {
  ok: { label: 'OK', glyph: '●', tone: 'up' },
  off: { label: 'Off', glyph: '○', tone: 'idle' },
  stale: { label: 'Stale', glyph: '◆', tone: 'warn' },
  restarting: { label: 'Restarting', glyph: '↻', tone: 'warn' },
  dead: { label: 'Dead', glyph: '▼', tone: 'down' },
  gave_up: { label: 'Gave up', glyph: '▼', tone: 'down' },
  unknown: { label: 'Unknown', glyph: '○', tone: 'idle' }
};

const KIND_LABEL = { broker: 'Broker', llm: 'LLM', model: 'Model', data: 'Data', feed: 'Feed' };
export const KIND_ORDER = ['broker', 'llm', 'model', 'data', 'feed'];

// Friendly names for the sources mason_health registers today (_DEFAULT_CHECKS). Display only: an unknown
// name still works (derived below), so a source added on the backend needs no UI change.
const NAMES = {
  alpaca_paper: ['Alpaca Paper', 'ALPACA'],
  ollama_qwen: ['Ollama qwen', 'QWEN'],
  yfinance: ['yfinance', 'YF'],
  fmp: ['FMP', 'FMP'],
  alpha_vantage: ['Alpha Vantage', 'AV'],
  congress_gov: ['Congress.gov', 'CONG'],
  alpaca_news_ws: ['Alpaca News WS', 'NEWS'],
  gdelt: ['GDELT', 'GDELT'],
  sec_edgar: ['SEC EDGAR', 'SEC'],
  telegram: ['Telegram', 'TG'],
  calendar_fed: ['Fed calendar', 'FED'],
  calendar_econ: ['Econ calendar', 'ECON'],
  local_model_gpu: ['Local AI model', 'LOCAL AI']
};

const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
const fin = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v, max = 600) => {
  if (v == null) return '';
  const s = String(v);
  return s.length > max ? `${s.slice(0, max)}…` : s;
};

export function displayName(name) {
  const k = String(name || '');
  if (hasOwn(NAMES, k)) return NAMES[k][0]; // own keys only: a name like "constructor" is not a friendly name
  const words = k.split(/[_\-\s]+/).filter(Boolean);
  if (!words.length) return '?';
  return words.map(w => (w.length <= 3 ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1))).join(' ');
}

export function shortName(name) {
  const k = String(name || '');
  if (hasOwn(NAMES, k)) return NAMES[k][1];
  const first = k.split(/[_\-\s]+/).filter(Boolean)[0] || '?';
  return first.slice(0, 5).toUpperCase();
}

export function kindLabel(kind) {
  const k = String(kind || '');
  return (hasOwn(KIND_LABEL, k) && KIND_LABEL[k]) || (k ? k[0].toUpperCase() + k.slice(1) : 'Other');
}

/** Same rule as the template's healthState(): the explicit sub-state first, else derived from ok. */
export function stateOf(s) {
  const st = typeof s.state === 'string' ? s.state.toLowerCase() : '';
  if (st === 'up' || st === 'down' || st === 'limit') return st;
  // R2-A 2026-11-16 / R3-C 2026-11-17: an optional source with no key (ok null) gets its own calm state,
  // never down and never "not checked", whatever ok says
  if (st === 'not_configured') return 'unconfigured';
  // APIGPU: the local model's own states (on_gpu / idle / on_cpu / gpu_missing, and APIAMBER's split of on_gpu), on
  // the model source only; anywhere else they fall through to the ok rule like any other unknown sub-state
  const key = isModelSource(s) ? modelKey(s) : null;
  if (key) return MODEL_META[key].state;
  if (s.ok === true) return 'up';
  if (s.ok === false) return 'down';
  return 'unknown';
}

/** A finite number inside [lo, hi], else null (server numbers are never trusted to be numbers). */
const within = (v, lo, hi) => {
  const n = fin(v);
  return n != null && n >= lo && n <= hi ? n : null;
};
/** A memory figure in MB (vram_mb / size_mb), else null. */
const mbOf = v => within(v, 0, 1e7);

/** APIAMBER: less than half of the model on the graphics card, by the backend's own split (mason_health.py
 *  `vram * 2 < size`). The backend splits on bytes and sends whole MB, so a reading within about 1 MB of exactly half
 *  could fall on the other side here. False when either number is missing (then the page can't tell). */
export function mostlyOnCpu(vramMb, sizeMb) {
  return vramMb != null && sizeMb != null && sizeMb > 0 && vramMb * 2 < sizeMb;
}

/** The MODEL_META key for a raw local-model source, or null when the page has no words for its state: one of the
 *  backend's five states, except on_gpu with less than half of the model on the card → 'mostly_cpu' (APIAMBER). */
function modelKey(s) {
  const raw = typeof s.state === 'string' ? s.state.toLowerCase() : '';
  if (!BACKEND_MODEL_STATES.includes(raw)) return null;
  if (raw === 'on_gpu' && mostlyOnCpu(mbOf(s.vram_mb), mbOf(s.size_mb))) return 'mostly_cpu';
  return raw;
}
/** A non-empty string (trimmed, capped), else null. Numbers / objects are not names. */
const text = (v, max) => {
  if (typeof v !== 'string') return null;
  const s = v.trim();
  if (!s) return null;
  return s.length > max ? `${s.slice(0, max)}…` : s;
};

/** ISO date-time → epoch seconds, or null. Python's 6-digit fractions are cut to 3 for Date.parse. */
export function isoEpoch(iso) {
  if (typeof iso !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(iso)) return null;
  const ms = Date.parse(iso.replace(/(\.\d{3})\d+/, '$1'));
  return Number.isFinite(ms) ? ms / 1000 : null;
}

/** Epoch seconds → "YYYY-MM-DDTHH:MM:SS" in New York time (what etClock reads), or null. */
export function etIsoOf(epochSec) {
  if (epochSec == null || !Number.isFinite(epochSec)) return null;
  try {
    const parts = {};
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
    for (const p of fmt.formatToParts(new Date(epochSec * 1000))) parts[p.type] = p.value;
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
  } catch {
    return null;
  }
}

/** The local model's extra fields (all optional), validated. `state` is set only for a state we have words for:
 *  a MODEL_META key (the backend's state, or APIAMBER's 'mostly_cpu'); the server's own name stays in `rawState`. */
function normModel(s) {
  const key = modelKey(s);
  return {
    state: key,
    known: key != null,
    look: key ? MODEL_META[key].look : null,
    name: text(s.model, 80),
    vramMb: mbOf(s.vram_mb),
    sizeMb: mbOf(s.size_mb),
    gpuName: text(s.gpu_name, 80),
    tempC: within(s.temp_c, -40, 150),
    powerW: within(s.power_w, 0, 5000),
    powerLimitW: within(s.power_limit_w, 1, 5000) // a limit of 0 W means "not known"
  };
}

function normSource(s) {
  const name = str(s.name, 64) || '?';
  const kind = str(s.kind, 24).toLowerCase();
  const state = stateOf(s);
  // APIGPU: a check that is not one of the monitor's own (no last_checked) may stamp itself with checked_at (ISO)
  const lastChecked = fin(s.last_checked) != null ? s.last_checked : isoEpoch(s.checked_at);
  const lastCheckedIso =
    typeof s.last_checked_iso === 'string' ? s.last_checked_iso : fin(s.last_checked) == null ? etIsoOf(lastChecked) : null;
  const out = {
    name,
    label: displayName(name),
    short: shortName(name),
    kind,
    kindLabel: kindLabel(kind),
    state,
    notConfigured: state === 'unconfigured',
    rawState: typeof s.state === 'string' ? str(s.state, 24) : null,
    ok: s.ok === true ? true : s.ok === false ? false : null,
    latencyMs: fin(s.latency_ms) != null && s.latency_ms >= 0 ? s.latency_ms : null,
    detail: str(s.detail),
    note: str(s.note, 200),
    lastChecked,
    lastCheckedIso,
    since: fin(s.since),
    sinceIso: typeof s.since_iso === 'string' ? s.since_iso : null,
    big: kind === 'broker' || kind === 'llm' || kind === MODEL_KIND
  };
  // only the local-model source carries `model` (every other source keeps exactly its old shape)
  if (isModelSource({ name, kind })) out.model = normModel(s);
  return out;
}

/** Move the local-model source(s) right after the LLM sources, so the model sits next to Ollama in the orbit
 *  and in the list (the backend sorts unknown kinds last). Everything else keeps the server's order. */
function placeModels(list) {
  const models = list.filter(s => s.model);
  if (!models.length) return list;
  const rest = list.filter(s => !s.model);
  let at = -1;
  rest.forEach((s, i) => {
    if (s.kind === 'llm') at = i;
  });
  if (at < 0) return list;
  return [...rest.slice(0, at + 1), ...models, ...rest.slice(at + 1)];
}

function normJob(j) {
  const status = typeof j.status === 'string' && JOB_META[j.status] ? j.status : 'unknown';
  return {
    name: str(j.name, 64) || '?',
    label: str(j.label, 160) || str(j.name, 64) || '?',
    status,
    every: fin(j.every_seconds),
    staleAfter: fin(j.stale_after_seconds),
    lastBeatAge: fin(j.last_beat_age_seconds),
    beats: fin(j.beats),
    busyWith: j.busy_with == null ? null : str(j.busy_with, 120),
    busyFor: fin(j.busy_for_seconds),
    restarts: fin(j.restarts_last_hour) || 0,
    lastError: j.last_error == null ? null : str(j.last_error, 300)
  };
}

function normJobs(b) {
  if (!isObj(b)) return null;
  const list = (Array.isArray(b.jobs) ? b.jobs : []).filter(isObj).slice(0, 64).map(normJob);
  const rc = isObj(b.reconcile) ? b.reconcile : null;
  const wd = isObj(b.watchdog) ? b.watchdog : null;
  const counts = { ok: 0, off: 0, warn: 0, bad: 0 };
  for (const j of list) {
    if (j.status === 'ok') counts.ok += 1;
    else if (j.status === 'off' || j.status === 'unknown') counts.off += 1;
    else if (j.status === 'stale' || j.status === 'restarting') counts.warn += 1;
    else counts.bad += 1;
  }
  return {
    ok: b.ok === true ? true : b.ok === false ? false : null,
    error: b.error ? str(b.error, 300) : null,
    list,
    counts,
    problems: list.filter(j => j.status !== 'ok' && j.status !== 'off'),
    supervisorRunning: b.supervisor_running === true ? true : b.supervisor_running === false ? false : null,
    watchdog: wd ? { enabled: wd.enabled === true, pings: fin(wd.pings) || 0 } : null,
    reconcile: rc
      ? {
          at: typeof rc.at === 'string' ? rc.at : null,
          kind: str(rc.kind, 40),
          ok: rc.ok === true ? true : rc.ok === false ? false : null,
          fillsSeen: fin(rc.fills_seen),
          journalSynced: fin(rc.journal_synced),
          errors: Array.isArray(rc.errors) ? rc.errors.length : 0
        }
      : null
  };
}

/**
 * /api/health body → view model, or null for a body that is not an object.
 * `prev` (the last good model) enables structural sharing: an unchanged source keeps its object, so
 * memoized rows and the canvas engine see "nothing changed" on an identical poll.
 */
export function normalizeHealth(body, fetchedAt = Date.now(), prev = null) {
  if (!isObj(body)) return null;
  const gen = fin(body.generated_at);
  const seen = new Set();
  const prevBy = new Map(prev && Array.isArray(prev.sources) ? prev.sources.map(s => [s.name, s]) : []);
  const list = [];
  for (const raw of (Array.isArray(body.sources) ? body.sources : []).filter(isObj).slice(0, 64)) {
    const s = normSource(raw);
    if (seen.has(s.name)) continue;
    seen.add(s.name);
    const old = prevBy.get(s.name);
    list.push(old && JSON.stringify(old) === JSON.stringify(s) ? old : s);
  }
  const sources = placeModels(list);
  const counts = { up: 0, limit: 0, down: 0, unknown: 0, unconfigured: 0, slow: 0, total: sources.length, active: 0 };
  for (const s of sources) counts[s.state] += 1;
  // active = the sources that are expected to work (not-configured ones are optional and left out)
  counts.active = counts.total - counts.unconfigured;
  const kinds = [];
  for (const s of sources) if (!kinds.includes(s.kind)) kinds.push(s.kind);
  kinds.sort((a, b) => {
    const ia = KIND_ORDER.indexOf(a);
    const ib = KIND_ORDER.indexOf(b);
    return (ia < 0 ? 9 : ia) - (ib < 0 ? 9 : ib) || a.localeCompare(b);
  });
  const byKind = kinds.map(kind => {
    const k = { kind, label: kindLabel(kind), total: 0, up: 0, limit: 0, down: 0, unknown: 0, unconfigured: 0, slow: 0 };
    for (const s of sources) {
      if (s.kind !== kind) continue;
      k.total += 1;
      k[s.state] += 1;
    }
    return k;
  });
  return {
    sources,
    counts,
    byKind,
    generatedAt: gen,
    // server clock - client clock (ms): ages are measured on the server's clock, so a skewed client
    // clock never shows "checked -3m ago"
    skewMs: gen != null ? Math.round(gen * 1000 - fetchedAt) : 0,
    fetchedAt,
    monitorNote: body.detail ? str(body.detail, 200) : null,
    error: body.error ? str(body.error, 300) : null,
    jobs: normJobs(body.background_jobs),
    // APIGPU: the local-model source when the backend reports it, else null (then nothing about it is shown)
    localModel: sources.find(s => s.model) || null
  };
}

/** The words / glyph / tone a source shows: the local model's own (MODEL_META) for a state we know, else the
 *  health state's (STATE_META). Always an object. */
export function metaOf(src) {
  if (src && src.model && src.model.known) return MODEL_META[src.model.state];
  const st = src && src.state;
  return (typeof st === 'string' && hasOwn(STATE_META, st) && STATE_META[st]) || STATE_META.unknown;
}

/** The visual state the constellation draws: the model's look ("asleep" / "slow" / …) or the health state. */
export const lookOf = src => (src && src.model && src.model.known ? src.model.look : src ? src.state : 'unknown');

/** A source's state as text: "Not configured" / "Up" / "On the graphics card" / … (long) or the chip's short form. */
export function stateLabel(src, { short = false } = {}) {
  const m = typeof src === 'string' ? (hasOwn(STATE_META, src) && STATE_META[src]) || STATE_META.unknown : metaOf(src);
  return short ? m.short : m.label;
}

/** True when a source belongs on the Issues filter / issue counts: limit, down, not checked. Never up or
 *  not configured (an optional source without a key is a choice, not a problem). */
export const isIssue = src => !!src && src.state !== 'up' && src.state !== 'unconfigured';

/** Source-detail filter rule: Issues = limit / down / not checked; Up = up; All = everything (incl. not configured). */
export const keepSource = (filter, src) => (filter === 'issues' ? isIssue(src) : filter === 'up' ? src.state === 'up' : true);

/** Count per Source-detail filter, plus the not-configured ones (shown under All only). */
export const filterCounts = sources => ({
  all: sources.length,
  issues: sources.filter(isIssue).length,
  up: sources.filter(s => s.state === 'up').length,
  unconfigured: sources.filter(s => s.state === 'unconfigured').length
});

/** "Up for" / "Waiting" / "Not configured for" / "In state": the label in front of a source's state age.
 *  Takes a state, or a source (the local model then says "On the card for" / "Asleep for" / …). */
export function inStateLabel(srcOrState) {
  if (srcOrState && typeof srcOrState === 'object') {
    if (srcOrState.model && srcOrState.model.known) return MODEL_META[srcOrState.model.state].inState;
    return inStateLabel(srcOrState.state);
  }
  const state = srcOrState;
  if (state === 'up') return 'Up for';
  if (state === 'unknown') return 'Waiting';
  if (state === 'unconfigured') return 'Not configured for';
  return 'In state';
}

/** Server "now" in epoch seconds for a model (client clock corrected by the snapshot's skew). */
export const serverNow = (model, clientNow = Date.now()) => (clientNow + ((model && model.skewMs) || 0)) / 1000;

/** Seconds since an epoch-seconds stamp, on the server clock; null when unknown. */
export function ageOf(model, epochSec, clientNow = Date.now()) {
  if (epochSec == null) return null;
  return Math.max(0, serverNow(model, clientNow) - epochSec);
}

/** Seconds since a job's last beat, now (the snapshot's age plus the time since we fetched it). */
export function jobBeatAge(model, job, clientNow = Date.now()) {
  if (!job || job.lastBeatAge == null) return null;
  return Math.max(0, job.lastBeatAge + (clientNow - ((model && model.fetchedAt) || clientNow)) / 1000);
}

/** 42 s → "42s", 200 → "3m", 7900 → "2h 11m", 3 days → "3d 4h". */
export function ageText(sec) {
  if (sec == null || !Number.isFinite(sec)) return '—';
  const s = Math.max(0, Math.round(sec));
  if (s < 5) return 'now';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

export const agoText = sec => {
  const t = ageText(sec);
  return t === '—' || t === 'now' ? t : `${t} ago`;
};

/** "2026-11-16T15:34:22.1-05:00" (mason_health writes ET) → "3:34 PM ET"; with the date when not `dayIso`. */
export function etClock(iso, dayIso = null) {
  const m = typeof iso === 'string' ? iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/) : null;
  if (!m) return null;
  const h = Number(m[4]);
  const t = `${h % 12 || 12}:${m[5]} ${h < 12 ? 'AM' : 'PM'} ET`;
  const day = `${m[1]}-${m[2]}-${m[3]}`;
  if (dayIso && day === dayIso.slice(0, 10)) return t;
  if (!dayIso) return t;
  const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][Number(m[2]) - 1] || m[2];
  return `${mon} ${Number(m[3])}, ${t}`;
}

/** "182 ms" / "1.4 s" / "—"; a real reading that rounds to zero is "under 1 ms", never "0 ms".
 *  UIFIX 2026-11-18: the backend sends whole or tenth milliseconds, so a very fast local reply (Ollama's status
 *  answer for the local AI model) arrives as 0 or 0.3. "0 ms" read like a number nobody measured. */
export const UNDER_1_MS = 'under 1 ms';
/** True for a real reading (finite, not negative) that would round to 0 ms. */
export const underOneMs = ms => ms != null && Number.isFinite(ms) && ms >= 0 && Math.round(ms) < 1;
export function latencyText(ms) {
  if (ms == null || !Number.isFinite(ms)) return '—';
  if (underOneMs(ms)) return UNDER_1_MS;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}

/** Seconds a data pulse takes to cross its link: fast links (low latency) pulse fast. Log-shaped, 1-7 s. */
export function pulseSeconds(latencyMs) {
  if (latencyMs == null || !Number.isFinite(latencyMs)) return 2.4;
  return Math.max(1, Math.min(7, 0.9 + 1.25 * Math.log(1 + Math.max(0, latencyMs) / 60)));
}

/** APIGPU: pulse period on a "slow" link (the model on the processor): ten times the normal one, 8-30 s, so the
 *  amber pulses visibly crawl next to the green ones. */
export const slowPulseSeconds = latencyMs => Math.max(8, Math.min(30, 10 * pulseSeconds(latencyMs)));

// ── APIGPU: the local model's numbers as plain text (only the numbers the backend sent) ─────────────────────
/** 3342 → "3.3 GB", 850 → "850 MB". The backend's MB are decimal (1 MB = 1,000,000 bytes, like `ollama ps`), so
 *  1 GB = 1000 MB here: the same figures as the backend's own sentence in `detail`. */
export function mbText(mb, digits = 1) {
  if (mb == null || !Number.isFinite(mb)) return '—';
  if (mb >= 1000) return `${(mb / 1000).toFixed(digits)} GB`;
  return `${Math.round(mb)} MB`;
}

/** Share of the model held in graphics-card memory, 0-1, or null when either number is missing. */
export function memShare(m) {
  if (!m || m.vramMb == null || m.sizeMb == null || !(m.sizeMb > 0)) return null;
  return Math.max(0, Math.min(1, m.vramMb / m.sizeMb));
}

/** REVIEW 2026-11-17: the local model's "what this means" line, true for the numbers it was sent. On the card it only
 *  says "at full speed" when all of the model is on the card. Part of it on the processor = "a little slower". Less than
 *  half on the card is its own amber state since APIAMBER (mostly_cpu), and its hint keeps the same words: "mostly on
 *  the processor, much slower". null for a source without the model's own words. */
export function modelHint(src) {
  if (!src || !src.model || !src.model.known) return null;
  const m = src.model;
  if (m.state === 'on_gpu') {
    const share = memShare(m);
    if (share == null) return 'Running on the graphics card.';
    if (share < 1) return 'Running on the graphics card, with part of it on the processor, so it can be a little slower.';
  }
  return MODEL_META[m.state].hint;
}

/** "3.3 GB, all on the card" / "4.9 of 5.6 GB on the card" / "3.3 GB, none on the card" / "3.3 GB on the card".
 *  A split under 0.1 GB gets two decimals ("3.29 of 3.34 GB"), like the backend's sentence. */
export function memoryText(m) {
  if (!m) return null;
  const { vramMb: v, sizeMb: s } = m;
  if (v != null && s != null && s > 0) {
    if (v >= s) return `${mbText(s)}, all on the card`;
    if (v <= 0) return `${mbText(s)}, none on the card`;
    const d = s - v < 100 ? 2 : 1;
    const pair = v >= 1000 && s >= 1000 ? `${(v / 1000).toFixed(d)} of ${mbText(s, d)}` : `${mbText(v)} of ${mbText(s, d)}`;
    return `${pair} on the card`;
  }
  if (v != null && v > 0) return `${mbText(v)} on the card`;
  if (s != null && s > 0) return `${mbText(s)} loaded`;
  return null;
}

export const tempText = c => (c == null || !Number.isFinite(c) ? '—' : `${Math.round(c)} °C`);

/** "61 W of 160 W" / "61 W". */
export function powerText(m) {
  if (!m || m.powerW == null) return null;
  const w = `${Math.round(m.powerW)} W`;
  return m.powerLimitW != null ? `${w} of ${Math.round(m.powerLimitW)} W` : w;
}

export function powerShare(m) {
  if (!m || m.powerW == null || m.powerLimitW == null) return null;
  return Math.max(0, Math.min(1, m.powerW / m.powerLimitW));
}

/**
 * The model's facts for the tooltip / card / list row / summary, in reading order, only those present:
 * [{k: 'model'|'memory'|'temp'|'power'|'card', label, text, share?, hot?}].
 */
export function modelFacts(src) {
  const m = src && src.model;
  if (!m) return [];
  const out = [];
  if (m.name) out.push({ k: 'model', label: 'Model', text: m.name });
  const mem = memoryText(m);
  if (mem) out.push({ k: 'memory', label: 'Memory', text: mem, share: memShare(m) });
  if (m.tempC != null) out.push({ k: 'temp', label: 'Temperature', text: tempText(m.tempC), hot: m.tempC >= HOT_C });
  const pw = powerText(m);
  if (pw) out.push({ k: 'power', label: 'Power', text: pw, share: powerShare(m) });
  if (m.gpuName) out.push({ k: 'card', label: 'Card', text: m.gpuName });
  return out;
}

/** 0.45-1 brightness from the age of the last check (fresh = bright). */
export function freshness(ageSec) {
  if (ageSec == null || !Number.isFinite(ageSec)) return 0.7;
  if (ageSec <= 600) return 1;
  return Math.max(0.45, 1 - 0.55 * Math.min(1, Math.log(ageSec / 600) / Math.log(6)));
}

/** "every 30s" / "every 6h" for a job interval. */
export const everyText = sec => (sec == null ? '—' : `every ${ageText(sec) === 'now' ? `${Math.round(sec)}s` : ageText(sec)}`);

/** APIAMBER: what "◆ slow" means in the constellation legend's hover text: the local model's own words (on the
 *  processor / mostly on the processor), else the generic ones. */
export function slowWords(model) {
  const lm = model && model.localModel;
  const m = lm && lm.state === 'slow' ? metaOf(lm) : null;
  return (m && m.legend) || 'something is working, but much slower than it should be';
}

/** Overall headline for the summary (text + tone + glyph; never color alone). */
export function headline(model, { failed = false, status = 'ok' } = {}) {
  if (!model) {
    if (status === 'error') return { text: 'Health feed unreachable', tone: 'down', glyph: '▼' };
    return { text: 'Connecting…', tone: 'idle', glyph: '○' };
  }
  const c = model.counts;
  const jobsBad = model.jobs ? model.jobs.problems.length : 0;
  const sub = jobsBad ? `${jobsBad} background loop${jobsBad === 1 ? ' needs' : 's need'} attention` : '';
  if (!c.total) {
    if (model.error) return { text: 'Health snapshot failed', tone: 'down', glyph: '▼', sub };
    return { text: 'Monitor starting', tone: 'idle', glyph: '○', sub };
  }
  // APIGPU: the local AI model speaks for itself. On the processor it leads the headline (the last outage ran
  // 17.5 h on the processor while this page stayed green); a missing card / Ollama down adds a note under
  // "N links down". `notes` = extra lines under the headline: [{text, tone, glyph}] (glyph + words, never color alone).
  const lm = model.localModel;
  const lmMeta = lm && lm.model && lm.model.known ? MODEL_META[lm.model.state] : null;
  const lmNote = lmMeta && lmMeta.note ? { text: `Local AI model: ${lmMeta.note}`, tone: lmMeta.tone, glyph: lmMeta.glyph } : null;
  const limitText = `${c.limit} link${c.limit === 1 ? '' : 's'} rate-limited`;
  // not-configured sources are optional: they never make the headline alarming, and "x of y" counts only the
  // active ones (all configured links up → "All links nominal", the count of unset ones shows on its own)
  if (c.down) return { text: `${c.down} link${c.down === 1 ? '' : 's'} down`, tone: 'down', glyph: '▼', sub, notes: lmNote ? [lmNote] : [] };
  if (c.slow) {
    const own = c.slow === 1 && !!lm && lm.state === 'slow';
    // short words in the headline (it must fit the widget's minimum width); the note says where and how much: the
    // model's own `lead` (APIAMBER: on the processor, or mostly on the processor)
    const notes = own && lmMeta && lmMeta.lead ? [{ text: lmMeta.lead, tone: 'warn', glyph: '◆' }] : [];
    if (c.limit) notes.push({ text: limitText, tone: 'warn', glyph: '◆' });
    return { text: own ? 'Local AI running slow' : `${c.slow} link${c.slow === 1 ? '' : 's'} slow`, tone: 'warn', glyph: '◆', sub, notes };
  }
  if (c.limit) return { text: limitText, tone: 'warn', glyph: '◆', sub };
  if (jobsBad) return { text: 'All links up', tone: 'warn', glyph: '◆', sub };
  if (c.unknown) return { text: `${c.up} of ${c.active != null ? c.active : c.total} links up`, tone: 'idle', glyph: '○', sub };
  if (c.active === 0) return { text: 'No links configured', tone: 'idle', glyph: '◌', sub };
  return { text: failed ? 'All links nominal (stale)' : 'All links nominal', tone: 'up', glyph: '▲', sub };
}
