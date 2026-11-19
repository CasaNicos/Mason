"""
mason_db.py — Mason's SQLite connection + schema for the trade / options journals.

Both mason_trades.py and mason_options.py get their persistence from here instead
of reading/writing trades_journal.json / options_journal.json directly. This is the
ONE place the schema — including the soft-delete columns and the active_* views —
is defined.

Design notes
------------
* SQLite, single file (mason.db) next to mason.config.json. Same convention as the
  JSON state files it replaces. No new pip dependency — sqlite3 is stdlib.
* A FRESH connection is returned per get_connection() call. Flask serves requests
  on multiple threads and a shared sqlite3.Connection is not thread-safe; a
  connection-per-call sidesteps that entirely. Connections are cheap against a
  local file and get closed by GC once the caller drops them.
* Every table carries a who/when audit trail: created_at + created_by on the way
  in, and is_deleted / deleted_at / deleted_reason / deleted_by on the way out
  (reminders uses its own createddate/createdby/deleted_date/deletedby spelling —
  same idea, older column names). created_by / deleted_by are one of 'nico',
  'mason', or 'system'.
* Soft delete, never physical delete. The active_* VIEWS filter is_deleted = 0, and every function
  that hands trade data to the model reads through those views — a soft-deleted
  row is structurally incapable of coming back out (see mason_trades.get_open_trades
  et al.). The raw tables are only reachable by a function that asks for a single
  row by exact id, or one that passes include_deleted=True on purpose.
* extra_json: a catch-all TEXT column on both tables. mason_options.log_play() does
  `play.update(play_data)` with no key whitelist, so callers have historically
  added fields the canonical row shape never declared ("chassis", "repair_note").
  Any key not backed by a real column is round-tripped through extra_json so
  nothing a caller wrote is ever silently dropped.
"""

import json as _json
import sqlite3
from datetime import datetime, date
from pathlib import Path

# Redirectable so the test suites can point the whole module at a temp file.
# Referenced live inside get_connection(), never captured at import time by callers.
DB_FILE = Path(__file__).parent / "mason.db"

_SOFT_DELETE_COLS = ("is_deleted", "deleted_at", "deleted_reason", "deleted_by")
_INTERNAL_COLS = ("extra_json",) + _SOFT_DELETE_COLS

# ── Per-table config. `columns` is the exact ordered column list; row_to_dict()
#    and the upsert helper are driven off it so there is no second copy of the
#    schema to drift. ────────────────────────────────────────────────────────────
TABLES = {
    "trades": {
        "view": "active_trades",
        "json_fields": set(),
        "bool_fields": set(),
        "columns": (
            "id", "created_at", "created_by", "entry_date", "status", "ticker", "direction",
            "setup_type", "time_horizon", "shares", "entry_price", "stop_loss",
            "target_price", "risk_per_share", "reward_per_share", "risk_reward_ratio",
            "thesis", "mason_confidence", "notes", "outcome", "exit_price",
            "exit_date", "actual_pnl", "exit_reason", "review_text", "thesis_score",
            "thesis_score_notes", "entry_order_id", "exit_order_id",
            "is_deleted", "deleted_at", "deleted_reason", "deleted_by", "extra_json",
        ),
    },
    "options_trades": {
        "view": "active_options_trades",
        "json_fields": {"strikes", "hold_directive"},
        "bool_fields": {"execution_verified"},
        "columns": (
            "id", "created_at", "created_by", "entry_date", "status", "ticker", "strategy",
            "direction", "expiration", "strikes", "premium", "premium_type",
            "contracts", "underlying_price_at_entry", "iv_at_entry", "thesis",
            "mason_confidence", "notes", "max_profit", "max_loss", "breakeven",
            "outcome", "actual_pnl", "underlying_price_at_expiry", "expiry_review",
            "expiry_review_date", "review_30d", "review_30d_date", "review_60d",
            "review_60d_date", "thesis_score", "thesis_score_notes", "hold_directive",
            "execution_verified", "order_id", "client_order_id", "contract_symbol",
            "filled_qty", "filled_avg_price", "filled_at", "execution_note",
            "exit_order_id", "exit_client_order_id", "exit_filled_qty",
            "exit_filled_avg_price", "exit_filled_at",
            "is_deleted", "deleted_at", "deleted_reason", "deleted_by", "extra_json",
        ),
    },
    "memory_facts": {
        "view": "active_memory_facts",
        "json_fields": set(),
        "bool_fields": set(),
        "columns": (
            "id", "text", "tag", "created_at", "created_by", "updated_at",
            "is_deleted", "deleted_at", "deleted_reason", "deleted_by", "extra_json",
        ),
    },
    "personal_positions": {
        "view": "active_personal_positions",
        "json_fields": set(),
        "bool_fields": set(),
        "columns": (
            "id", "created_at", "created_by", "updated_at", "ticker", "account_label",
            "shares", "cost_basis", "entry_date", "status", "pending_action",
            "pending_close_price", "pending_close_date", "close_date", "close_price",
            "realized_pl", "notes",
            "is_deleted", "deleted_at", "deleted_reason", "deleted_by", "extra_json",
        ),
    },
}

_SCHEMA = """
PRAGMA journal_mode = WAL;   -- readers don't block the writer; matters once the
                             -- mason_llm.py local-model gateway is calling in too.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS trades (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    created_by          TEXT,                           -- who logged it: 'nico' | 'mason' | 'system'
    entry_date          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',   -- open | closed | reviewed
    ticker              TEXT NOT NULL,
    direction           TEXT NOT NULL,                  -- long | short
    setup_type          TEXT,
    time_horizon        TEXT,
    shares              REAL,
    entry_price         REAL,
    stop_loss           REAL,
    target_price        REAL,
    risk_per_share      REAL,
    reward_per_share    REAL,
    risk_reward_ratio   REAL,
    thesis              TEXT,
    mason_confidence    INTEGER,
    notes               TEXT,
    outcome             TEXT,                           -- win | loss | breakeven
    exit_price          REAL,
    exit_date           TEXT,
    actual_pnl          REAL,
    exit_reason         TEXT,
    review_text         TEXT,
    thesis_score        INTEGER,
    thesis_score_notes  TEXT,
    entry_order_id      TEXT,
    exit_order_id       TEXT,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    deleted_at          TEXT,
    deleted_reason      TEXT,
    deleted_by          TEXT,                           -- who soft-deleted it: 'nico' | 'mason' | 'system'
    extra_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_active_status ON trades(status)      WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_trades_active_ticker ON trades(ticker)      WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_trades_active_setup  ON trades(setup_type)  WHERE is_deleted = 0;

CREATE VIEW IF NOT EXISTS active_trades AS
    SELECT * FROM trades WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS options_trades (
    id                          TEXT PRIMARY KEY,
    created_at                  TEXT NOT NULL,
    created_by                  TEXT,                          -- who logged it: 'nico' | 'mason' | 'system'
    entry_date                  TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'open',  -- open | closed_early | expired | reviewed
    ticker                      TEXT NOT NULL,
    strategy                    TEXT,
    direction                   TEXT,
    expiration                  TEXT,
    strikes                     TEXT,                          -- JSON: {"long_call": 1140, "short_call": 1150}
    premium                     REAL,
    premium_type                TEXT,                          -- debit | credit
    contracts                   INTEGER,
    underlying_price_at_entry   REAL,
    iv_at_entry                 REAL,
    thesis                      TEXT,
    mason_confidence            INTEGER,
    notes                       TEXT,
    max_profit                  REAL,
    max_loss                    REAL,
    breakeven                   REAL,
    outcome                     TEXT,                          -- win | loss | breakeven
    actual_pnl                  REAL,
    underlying_price_at_expiry  REAL,
    expiry_review               TEXT,
    expiry_review_date          TEXT,
    review_30d                  TEXT,
    review_30d_date             TEXT,
    review_60d                  TEXT,
    review_60d_date             TEXT,
    thesis_score                INTEGER,
    thesis_score_notes          TEXT,
    hold_directive              TEXT,                          -- JSON: {"hold_until":..., "reason":..., "set_at":..., "set_by":...}
    execution_verified          INTEGER NOT NULL DEFAULT 0,
    order_id                    TEXT,
    client_order_id             TEXT,
    contract_symbol             TEXT,
    filled_qty                  REAL,
    filled_avg_price            REAL,
    filled_at                   TEXT,
    execution_note              TEXT,
    exit_order_id               TEXT,
    exit_client_order_id        TEXT,
    exit_filled_qty             REAL,
    exit_filled_avg_price       REAL,
    exit_filled_at              TEXT,
    is_deleted                  INTEGER NOT NULL DEFAULT 0,
    deleted_at                  TEXT,
    deleted_reason              TEXT,
    deleted_by                  TEXT,                          -- who soft-deleted it: 'nico' | 'mason' | 'system'
    extra_json                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_options_active_status ON options_trades(status) WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_options_active_ticker ON options_trades(ticker) WHERE is_deleted = 0;

CREATE VIEW IF NOT EXISTS active_options_trades AS
    SELECT * FROM options_trades WHERE is_deleted = 0;

-- ── Personal positions: manually-entered holdings from Nico's own (non-Alpaca)
-- brokerage accounts, tracked with live market data the same way an Alpaca
-- position is. Each row is one lot — the same ticker bought twice in the same
-- account gets two rows, never auto-averaged, so cost basis and holding period
-- stay accurate per purchase. account_label distinguishes Nico's separate real
-- accounts (e.g. 'Schwab Brokerage') so the same ticker held
-- in two accounts at two different cost bases tracks two independent P/Ls.
--
-- pending_action / pending_close_* exist ONLY for the chat-entry path: a
-- position Mason parses out of conversation is staged (status='pending_confirm',
-- pending_action='add') and does not count as real until Nico explicitly
-- confirms it in the same conversation (see mason_personal.confirm_position).
-- A close initiated via chat stages the same way (pending_action='close') so a
-- misheard "I sold X" never silently closes a real position. Positions added or
-- closed through the UI form skip staging entirely — submitting the form IS the
-- confirming act.
CREATE TABLE IF NOT EXISTS personal_positions (
    id                   TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL,
    created_by           TEXT,                           -- who entered it: 'nico' | 'mason'
    updated_at           TEXT,
    ticker               TEXT NOT NULL,
    account_label        TEXT NOT NULL,                  -- e.g. 'Schwab Brokerage'
    shares               REAL NOT NULL,
    cost_basis           REAL NOT NULL,                  -- per-share entry price
    entry_date           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'open',   -- pending_confirm | open | closed
    pending_action       TEXT,                           -- NULL | 'add' | 'close'
    pending_close_price  REAL,
    pending_close_date   TEXT,
    close_date           TEXT,
    close_price          REAL,
    realized_pl          REAL,
    notes                TEXT,
    is_deleted           INTEGER NOT NULL DEFAULT 0,
    deleted_at           TEXT,
    deleted_reason       TEXT,
    deleted_by           TEXT,
    extra_json           TEXT
);

CREATE INDEX IF NOT EXISTS idx_personal_positions_active_status
    ON personal_positions(status) WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_personal_positions_active_account
    ON personal_positions(account_label) WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_personal_positions_active_ticker
    ON personal_positions(ticker) WHERE is_deleted = 0;

CREATE VIEW IF NOT EXISTS active_personal_positions AS
    SELECT * FROM personal_positions WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS memory_facts (
    id             TEXT PRIMARY KEY,
    text           TEXT NOT NULL,
    tag            TEXT NOT NULL DEFAULT 'note',
    created_at     TEXT NOT NULL,
    created_by     TEXT,                            -- who saved it: 'nico' | 'mason' | 'system'
    updated_at     TEXT,
    is_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_at     TEXT,
    deleted_reason TEXT,
    deleted_by     TEXT,                            -- who soft-deleted it: 'nico' | 'mason' | 'system'
    extra_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_active
    ON memory_facts(created_at) WHERE is_deleted = 0;

CREATE VIEW IF NOT EXISTS active_memory_facts AS
    SELECT * FROM memory_facts WHERE is_deleted = 0;

CREATE TABLE IF NOT EXISTS reminders (
    reminderid      TEXT PRIMARY KEY,
    createddate     TEXT NOT NULL,
    createdby       TEXT NOT NULL DEFAULT 'mason',   -- 'nico' | 'mason'
    reminder_detail TEXT NOT NULL,
    reminder_date   TEXT,                             -- nullable: a reminder can carry no date
    deleted_date    TEXT,
    deletedby       TEXT,                             -- who soft-deleted it: 'nico' | 'mason'
    record_deleted  INTEGER NOT NULL DEFAULT 0,       -- soft delete: hidden everywhere when 1
    completed       INTEGER NOT NULL DEFAULT 0,       -- checked off, but stays visible in the widget
    completed_date  TEXT,                             -- when it was checked off; soft-deleted 5d later
    modifieddate    TEXT,                            -- last text/date edit (NULL = never edited)
    modifiedby      TEXT,                             -- who edited it: 'nico' | 'mason'
    kind            TEXT NOT NULL DEFAULT 'reminder'  -- 'reminder' | 'note' (note = never spoken in the greeting/agenda)
);

CREATE INDEX IF NOT EXISTS idx_reminders_active
    ON reminders(reminder_date) WHERE record_deleted = 0;

-- ── News service (NEWS_SERVICE_DESIGN.md Phase 1) ─────────────────────────────
-- Durable store for the constantly-scanning news feed. Ingested by
-- mason_news_service.py; read by /api/news. Not part of the journal soft-delete
-- framework above — news is append-only, deduped by primary key (id).
CREATE TABLE IF NOT EXISTS news_events (
    id            TEXT PRIMARY KEY,   -- sha1(normalized headline)[:16] — dedup key
    headline      TEXT NOT NULL,
    url           TEXT,
    source        TEXT,               -- display name: "WSJ", "BBC", "Yahoo Finance"
    source_tier   INTEGER DEFAULT 2,  -- 1 wire/major, 2 general, 3 fill
    tickers       TEXT,               -- JSON array of symbols the headline touches
    category      TEXT,               -- holding | watchlist | macro | global
    impact        TEXT,               -- red | yellow | green (instant heuristic)
    materiality   REAL,               -- 0..1  (NULL until Phase 3 LLM scoring)
    direction     TEXT,               -- bullish | bearish | neutral (NULL until P3)
    implication   TEXT,               -- one-line book-relative take (NULL until P3)
    published_at  REAL,               -- source timestamp (epoch), may be NULL
    ingested_at   REAL NOT NULL,      -- when we received it (epoch)
    recv_latency  REAL,               -- ingested_at - published_at (seconds)
    scored        INTEGER DEFAULT 0,  -- 0 until the LLM pass runs (Phase 3)
    pinged        INTEGER DEFAULT 0,  -- 1 once folded into a brief (Phase 4, fire-once)
    entry_price   REAL,               -- primary ticker price when scored (Phase 4 calibration)
    calibrated    INTEGER DEFAULT 0,  -- 1 once the outcome was checked (Phase 4)
    raw           TEXT,               -- JSON blob of the original item
    -- Audit / soft-delete. News is append-only in normal operation, so these stay
    -- at their defaults; they exist so a row can be retired (spam/retraction) with
    -- the same who/when trail every other table carries. is_deleted defaults 0, so
    -- ingest behaviour is unchanged until something actually deletes.
    created_by    TEXT DEFAULT 'news_service',  -- ingest source of record
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    deleted_at    REAL,               -- epoch, matching this table's time columns
    deleted_by    TEXT                -- who retired it: 'nico' | 'mason' | 'system'
);

CREATE INDEX IF NOT EXISTS idx_news_events_recent
    ON news_events(ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_events_published
    ON news_events(published_at DESC);

CREATE TABLE IF NOT EXISTS news_sources (
    feed_url      TEXT PRIMARY KEY,
    source        TEXT NOT NULL,      -- display name shown on the bar
    tier          INTEGER DEFAULT 2,
    last_pub_date REAL,               -- watermark: newest published_at seen
    last_url      TEXT,               -- watermark: url of the newest item seen
    fail_streak   INTEGER DEFAULT 0,  -- consecutive fetch failures; feed rests after 5
    hits          INTEGER DEFAULT 0,  -- calibration (Phase 4): material events that moved
    misses        INTEGER DEFAULT 0,
    last_attempt  REAL,               -- epoch of last fetch attempt
    updated_at    REAL,
    -- Audit / soft-delete, same rationale as news_events above.
    created_by    TEXT DEFAULT 'news_service',
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    deleted_at    REAL,
    deleted_by    TEXT
);

-- ── Analyst Desk (ANALYST_DESK_PHASE1_PROMPT.md) ──────────────────────────────
-- Three new tables for the 24/7 analyst desk. Phase 1 populates `notifications`
-- (the durable typed inbox) and leaves `qwen_news_analysis` / `recommendations`
-- empty for Phases 2/3 so those phases are pure logic.
--
-- AUDIT CONVENTION (Nico's rule): these tables use the `reminders`-style
-- soft-delete spelling — created_date / created_by / record_deleted /
-- deleted_date / deleted_by — NOT the trade tables' created_at / is_deleted /
-- deleted_at spelling. Never physically delete a row; flip record_deleted = 1.
-- These tables are owned by mason_notifications.py (and future analyst modules),
-- NOT by the TABLES/upsert_rows machinery above (which is scoped to the journals'
-- is_deleted shape), so they are intentionally absent from the TABLES dict.

CREATE TABLE IF NOT EXISTS notifications (
    id             TEXT PRIMARY KEY,               -- uuid4 hex[:12]
    created_date   TEXT NOT NULL,                  -- ISO-8601
    created_by     TEXT,                           -- 'health' | 'news_service' | 'mason' | 'system'
    type           TEXT NOT NULL,                  -- 'source_status' | 'completed_analysis' | 'huge_event' | 'major_move' | 'recommendation' | 'system'
    severity       TEXT NOT NULL DEFAULT 'info',   -- 'info' | 'warn' | 'critical'
    title          TEXT NOT NULL,
    body           TEXT,
    ticker         TEXT,                           -- nullable
    ref_table      TEXT,                           -- 'news_events' | 'recommendations' | 'source' | NULL
    ref_id         TEXT,                           -- id in ref_table, or the source name
    is_read        INTEGER NOT NULL DEFAULT 0,
    read_date      TEXT,
    record_deleted INTEGER NOT NULL DEFAULT 0,     -- soft delete: hidden everywhere when 1
    deleted_date   TEXT,
    deleted_by     TEXT,
    extra_json     TEXT                            -- catch-all; carries dedup_key when set
);

CREATE INDEX IF NOT EXISTS idx_notifications_active
    ON notifications(record_deleted, created_date DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_type
    ON notifications(type);

CREATE TABLE IF NOT EXISTS qwen_news_analysis (
    id             TEXT PRIMARY KEY,               -- uuid4 hex[:12]
    created_date   TEXT NOT NULL,
    created_by     TEXT,                           -- 'mason' (qwen) in Phase 2
    news_id        TEXT,                           -- loose ref to news_events.id (no hard FK)
    ticker         TEXT,
    analysis_type  TEXT,                           -- 'ticker_deep' | 'sentiment' | 'history' | 'roundup'
    summary        TEXT,                           -- qwen's written analysis
    sentiment      TEXT,                           -- 'bullish' | 'bearish' | 'neutral' | NULL
    materiality    REAL,                           -- 0..1, may mirror news_events score
    details_json   TEXT,                           -- structured extras
    model          TEXT,                           -- 'qwen3.5:4b' etc.
    record_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_date   TEXT,
    deleted_by     TEXT,
    extra_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_qwen_news_analysis_news
    ON qwen_news_analysis(news_id);
CREATE INDEX IF NOT EXISTS idx_qwen_news_analysis_ticker
    ON qwen_news_analysis(record_deleted, ticker);

CREATE TABLE IF NOT EXISTS recommendations (
    id                   TEXT PRIMARY KEY,         -- uuid4 hex[:12]
    created_date         TEXT NOT NULL,
    created_by           TEXT,                     -- 'mason' (qwen) in Phase 3
    ticker               TEXT NOT NULL,
    kind                 TEXT,                     -- 'new_idea' | 'manage_position' | 'watch'
    direction            TEXT,                     -- 'long' | 'short' | 'trim' | 'exit' | 'watch'
    instrument           TEXT DEFAULT 'equity',
    action_text          TEXT,                     -- human-readable recommendation
    order_preview        TEXT,                     -- pre-staged `confirm order ...` line (Phase 3)
    materiality          REAL,
    source_news_id       TEXT,                     -- news_events.id (indexed)
    thesis_id            TEXT,                     -- Theses/ file id or debate id (Phase 3)
    status               TEXT DEFAULT 'open',      -- 'open' | 'superseded' | 'expired' | 'graded'
    outcome              TEXT,                     -- 'right' | 'wrong' | 'neutral' | NULL (Phase 3)
    outcome_checked_date TEXT,
    entry_ref_price      REAL,                     -- price when recommended
    eval_price           REAL,                     -- price at grading
    notes                TEXT,
    record_deleted       INTEGER NOT NULL DEFAULT 0,
    deleted_date         TEXT,
    deleted_by           TEXT,
    extra_json           TEXT
);

CREATE INDEX IF NOT EXISTS idx_recommendations_ticker
    ON recommendations(record_deleted, ticker);
CREATE INDEX IF NOT EXISTS idx_recommendations_status
    ON recommendations(status);
CREATE INDEX IF NOT EXISTS idx_recommendations_source
    ON recommendations(source_news_id);

-- ── Activity ledger: the append-only record of everything the models DO ───────
-- Owned by mason_activity.py. Distinct from `notifications` on purpose (Nico's
-- rule): notifications are the attention INBOX (read/unread, soft-delete, dedup);
-- activity is the WORK LEDGER — append-only, immutable, never read/unread, never
-- deleted. You scroll it, you don't clear it. Two categories share the table:
--   'analyst'  — qwen/opus cognition: scan, analysis_started, analysis_finished, flag
--   'plumbing' — gateway internals: route, retry, escalate, local_down, truncated
-- A row MAY link forward to the notification it produced (ref_table/ref_id), but
-- the link is optional: 'qwen began analysis' has nothing to point at yet.
-- `trigger` records what set the work off ('autonomous' loop vs a 'chat' request);
-- both are activity — the trigger just lets the UI tell them apart. Because it is
-- append-only this table intentionally has NO record_deleted / is_read columns.
CREATE TABLE IF NOT EXISTS activity (
    id             TEXT PRIMARY KEY,               -- uuid4 hex[:12]
    created_date   TEXT NOT NULL,                  -- ISO-8601
    actor          TEXT,                           -- 'qwen' | 'opus' | 'router' | 'system' | 'nico'
    category       TEXT NOT NULL DEFAULT 'analyst',-- 'analyst' | 'plumbing'
    type           TEXT NOT NULL,                  -- 'analysis_started' | 'analysis_finished' | 'flag' | 'scan' | 'chat_started' | 'route' | 'retry' | 'local_down' | ...
    title          TEXT NOT NULL,
    body           TEXT,
    ticker         TEXT,                           -- nullable
    trigger        TEXT NOT NULL DEFAULT 'autonomous', -- 'autonomous' | 'chat'
    ref_table      TEXT,                           -- forward link, e.g. 'notifications'
    ref_id         TEXT,                           -- id in ref_table (the notification this work produced)
    extra_json     TEXT                            -- catch-all: raw event fields, chat_id, etc.
);

CREATE INDEX IF NOT EXISTS idx_activity_feed
    ON activity(category, created_date DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type
    ON activity(type);
CREATE INDEX IF NOT EXISTS idx_activity_ticker
    ON activity(ticker);

-- ── Analyst Desk Phase 2: desk bookkeeping (key/value) ────────────────────────
-- Internal scheduler + watermark state for mason_analyst_desk.py. NOT user data
-- (no soft-delete columns) — just a durable key/value store the desk threads use
-- to stay idempotent and restart-safe. Keys: news_watermark (last ingested_at
-- processed), last_hourly_run, last_anchor_0700, last_anchor_1300, etc.
CREATE TABLE IF NOT EXISTS desk_state (
    key          TEXT PRIMARY KEY,
    value        TEXT,
    updated_date TEXT
);

-- ── Catalyst Desk (2026-09-25, docs/prompts/CATALYST_DESK_CONTRACTS.md) ─────
-- One row per STORY (a cluster of news_events about the same entity+theme).
-- Written by mason_triage; consumed by the desk (dossier queue) + director.
CREATE TABLE IF NOT EXISTS catalysts (
    id                 TEXT PRIMARY KEY,           -- uuid4 hex[:12]
    story_key          TEXT NOT NULL,              -- stable cluster key (see contracts §2)
    primary_ticker     TEXT,                       -- NULL for pure macro stories
    tickers            TEXT,                       -- JSON array
    event_kind         TEXT,                       -- contracts §1 vocabulary
    title              TEXT,                       -- representative headline
    first_seen         REAL,                       -- epoch of earliest linked event
    last_seen          REAL,                       -- epoch of latest linked event
    n_events           INTEGER DEFAULT 0,
    n_sources          INTEGER DEFAULT 0,
    velocity           REAL,                       -- recent rate vs baseline (ratio)
    market_materiality REAL,                       -- 0..1 max over linked events
    direction          TEXT,                       -- bullish | bearish | neutral | mixed
    tape_json          TEXT,                       -- {move_pct, vs_sector_pct, rel_volume, as_of}
    score              REAL,                       -- final catalyst score 0..1
    status             TEXT DEFAULT 'open',        -- open|alerted|dossier_ready|consumed|expired
    alerted_at         REAL,
    dossier_ids        TEXT,                       -- JSON array of dossiers.id
    consumed_by        TEXT,                       -- director rec id / anchor that used it
    extra_json         TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalysts_status ON catalysts(status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_catalysts_ticker ON catalysts(primary_ticker);
CREATE INDEX IF NOT EXISTS idx_catalysts_story  ON catalysts(story_key);

-- Deterministic research packs (no LLM). Written by mason_dossier; cached.
CREATE TABLE IF NOT EXISTS dossiers (
    id                 TEXT PRIMARY KEY,           -- uuid4 hex[:12]
    subject            TEXT NOT NULL,              -- ticker, or 'MACRO:<event_kind>'
    kind               TEXT NOT NULL,              -- ticker | macro
    schema_version     TEXT,                       -- 'dossier/1.0' | 'macro_dossier/1.0'
    built_at           REAL,                       -- epoch
    payload_json       TEXT,
    token_estimate     INTEGER,                    -- len(compact text)/4
    errors_json        TEXT,                       -- JSON array of section errors
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_dossiers_subject ON dossiers(subject, built_at DESC);

-- Scheduled future catalysts (IPO dates, PDUFA, trial completions, econ releases).
-- Written by mason_sources; read by triage (proximity boost) + dossier + director.
CREATE TABLE IF NOT EXISTS calendar_events (
    id                 TEXT PRIMARY KEY,           -- sha1(source|natural_key)[:16]
    source             TEXT,                       -- 'NASDAQ IPO' | 'ClinicalTrials.gov' | ...
    event_type         TEXT,                       -- ipo|ipo_pricing|pdufa|trial_completion|adcom|fda_decision|econ|earnings
    ticker             TEXT,
    title              TEXT,
    event_date         TEXT,                       -- ISO date (YYYY-MM-DD)
    event_time         TEXT,                       -- free text, may be NULL
    status             TEXT,                       -- source status (e.g. 'priced', 'RECRUITING')
    url                TEXT,
    details_json       TEXT,
    first_seen         REAL,
    last_updated       REAL,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_calendar_date   ON calendar_events(event_date);
CREATE INDEX IF NOT EXISTS idx_calendar_ticker ON calendar_events(ticker);

-- Director follow-ups (2026-09-27): things Opus DEFERS ("look again ahead of 10-29
-- earnings"). Opus has no memory between runs, so its JSON follow_ups are written
-- here deterministically (+ a user-visible reminders row) and re-injected into the
-- director package when due. Written/read by mason_director.
CREATE TABLE IF NOT EXISTS director_followups (
    id                 TEXT PRIMARY KEY,           -- uuid4 hex[:12]
    ticker             TEXT,                       -- NULL for macro follow-ups
    due_date           TEXT NOT NULL,              -- ISO date the director must revisit
    action             TEXT NOT NULL,              -- what to research/decide
    reason             TEXT,
    source_slate_id    TEXT,                       -- director slate that asked for it
    source_rec_id      TEXT,
    status             TEXT DEFAULT 'open',        -- open | surfaced | done | cancelled
    surfaced_slate_id  TEXT,                       -- run that re-read it when due
    surfaced_date      TEXT,
    reminder_id        TEXT,                       -- linked reminders.reminderid
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON director_followups(status, due_date);

-- Director memory (2026-09-27): one-line lessons Opus writes about its own CLOSED
-- predictions (in-band, in the next anchor call) — re-injected into later packages
-- so the director learns across runs. Written/read by mason_director.
CREATE TABLE IF NOT EXISTS director_lessons (
    id                 TEXT PRIMARY KEY,           -- uuid4 hex[:12]
    rec_id             TEXT,                       -- recommendations.id the lesson is about
    ticker             TEXT,
    event_kind         TEXT,                       -- catalyst kind of the original call, if any
    outcome            TEXT,                       -- right | wrong | neutral (at declared horizon)
    lesson             TEXT NOT NULL,
    tags               TEXT,                       -- JSON array (e.g. ["stale_catalyst","iv_rich"])
    source_slate_id    TEXT,                       -- run that wrote it
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_ticker ON director_lessons(ticker, created_date DESC);
CREATE INDEX IF NOT EXISTS idx_lessons_rec ON director_lessons(rec_id);

-- Theses (2026-09-28): long-lived "things we're watching for" — an early hint
-- (e.g. "NVDA to accelerate consumer GPU supply") recorded with the signals that
-- would CONFIRM or BREAK it, so a later headline can be matched back ("remember
-- when we discussed…"). Written by mason_theses (from Opus JSON, Nico via chat,
-- or qwen proposals); matched against new news by mason_theses.match_new_events.
CREATE TABLE IF NOT EXISTS theses (
    id                 TEXT PRIMARY KEY,           -- uuid4 hex[:12]
    tickers            TEXT,                       -- JSON array (may be empty for macro)
    title              TEXT NOT NULL,              -- short name ("NVDA consumer-GPU acceleration")
    thesis             TEXT NOT NULL,              -- the hypothesis in plain words
    direction          TEXT,                       -- bullish | bearish | neutral
    confirm_signals    TEXT,                       -- JSON array of phrases/entities that confirm
    break_signals      TEXT,                       -- JSON array that would invalidate
    source_refs        TEXT,                       -- JSON [{table,id,headline,date}] that started it
    origin             TEXT,                       -- opus_director | nico | qwen_proposal
    status             TEXT DEFAULT 'watching',    -- proposed|watching|confirmed|broken|expired|closed
    review_by          TEXT,                       -- ISO date to re-evaluate / expire
    linked_recs        TEXT,                       -- JSON array of recommendations.id
    last_match_date    TEXT,
    match_log          TEXT,                       -- JSON [{date, news_id, headline, verdict}]
    extra_json         TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_theses_status ON theses(status, review_by);

-- Latest snapshot per (source,key) for DIFF-based sources (app charts, odds).
CREATE TABLE IF NOT EXISTS source_snapshots (
    source             TEXT NOT NULL,
    key                TEXT NOT NULL,
    taken_at           REAL,
    payload_json       TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT,
    PRIMARY KEY (source, key)
);

-- ── Conversation transcript (2026-09-28) ─────────────────────────────────────
-- Append-only, human-readable record of every line exchanged with Nico: what he
-- typed and every line Mason showed/spoke (chat reply, greeting, spoken callout,
-- brief recap, direct answer, upload analysis). Written ONLY via
-- mason_transcript.log(). Kept indefinitely; no soft-delete columns (it's a log).
-- turn_id correlates with mason_trace.jsonl turn ids when a trace turn is active.
CREATE TABLE IF NOT EXISTS transcript (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,        -- ISO-8601 local, ms precision
    speaker   TEXT NOT NULL,        -- 'nico' | 'mason'
    channel   TEXT NOT NULL,        -- 'chat' | 'greeting' | 'callout' | 'brief' | 'direct_answer' | 'voice' | 'upload' | 'system'
    source    TEXT,                 -- route / code path, e.g. 'query_ai_stream'
    model     TEXT,                 -- model id or tier label, nullable
    turn_id   TEXT,                 -- mason_trace turn id, nullable
    text      TEXT NOT NULL,        -- exact full text
    meta_json TEXT                  -- extra fields (JSON)
);
CREATE INDEX IF NOT EXISTS idx_transcript_ts ON transcript(ts DESC);
CREATE INDEX IF NOT EXISTS idx_transcript_channel ON transcript(channel, ts DESC);

-- ── Watchlist (2026-09-30, docs/watchlist/WATCHLIST_CONTRACTS.md §1) ─────────
-- One source of truth for what Mason actually WATCHES (price-streamed), with who
-- put it there (origin nico|opus|qwen). Cap/swap rules live in mason_watchlist.py.
CREATE TABLE IF NOT EXISTS watchlist (
    id                 TEXT PRIMARY KEY,           -- uuid4 hex[:12]
    ticker             TEXT NOT NULL,
    origin             TEXT NOT NULL,              -- nico | opus | qwen (who decided)
    channel            TEXT,                       -- chat | ui | alpaca_app | director | seed | desk
    status             TEXT NOT NULL,              -- active | proposed | removed
    horizon            TEXT,                       -- intraday | swing | long
    reason             TEXT,
    levels_json        TEXT,                       -- JSON [{price, side, label, set_by}]
    thesis_id          TEXT,
    rec_id             TEXT,
    slate_id           TEXT,
    added_date         TEXT,
    added_by           TEXT,
    removed_date       TEXT,
    removed_by         TEXT,
    remove_reason      TEXT,
    replaced_by        TEXT,                       -- new item id when swapped out
    last_event_date    TEXT,
    event_count        INTEGER DEFAULT 0,
    extra_json         TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_date       TEXT,
    deleted_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_status ON watchlist(status, ticker);

-- append-only history of every watchlist mutation (never deleted — knowledge base)
CREATE TABLE IF NOT EXISTS watchlist_log (
    id                 TEXT PRIMARY KEY,
    ts                 TEXT,
    item_id            TEXT,
    ticker             TEXT,
    action             TEXT,                       -- add|remove|swap|update|refused|propose|promote|evict|sync_in|sync_out
    actor              TEXT,                       -- nico | opus | qwen | system
    reason             TEXT,
    slate_id           TEXT,
    detail_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_log_ticker ON watchlist_log(ticker, ts DESC);

CREATE TABLE IF NOT EXISTS watch_bars (
    ticker             TEXT NOT NULL,
    tf                 TEXT NOT NULL,              -- 1m | 5m | 1d
    ts                 INTEGER NOT NULL,           -- UTC epoch of bar START
    o REAL, h REAL, l REAL, c REAL, v REAL, vwap REAL, n INTEGER,
    source             TEXT,                       -- iex | sip | yf
    session            TEXT,                       -- pre | rth | post
    PRIMARY KEY (ticker, tf, ts)
);

CREATE TABLE IF NOT EXISTS watch_events (
    id                 TEXT PRIMARY KEY,
    ticker             TEXT,
    ts                 INTEGER,
    kind               TEXT,
    severity           TEXT,                       -- info | notable | alert
    value_json         TEXT,
    message            TEXT,
    notified           INTEGER DEFAULT 0,
    notification_id    TEXT,
    wake_candidate     INTEGER DEFAULT 0,
    consumed_by        TEXT,
    created_date       TEXT,
    created_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_events_ticker ON watch_events(ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_watch_events_ts ON watch_events(ts DESC);

CREATE TABLE IF NOT EXISTS watch_digests (
    id                 TEXT PRIMARY KEY,
    ticker             TEXT,
    date               TEXT,
    slot               TEXT,                       -- pre | mid | preanchor | close | event
    stats_json         TEXT,
    text               TEXT,
    model              TEXT,
    guard_ok           INTEGER,
    created_date       TEXT,
    created_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_digests_ticker ON watch_digests(ticker, date DESC);

CREATE TABLE IF NOT EXISTS watch_option_snaps (
    ticker             TEXT NOT NULL,
    ts                 INTEGER NOT NULL,
    expiry             TEXT,
    dte                INTEGER,
    atm_strike         REAL,
    atm_iv             REAL,
    straddle_mid       REAL,
    implied_move_pct   REAL,
    underlying         REAL,
    source             TEXT,
    PRIMARY KEY (ticker, ts)
);

-- ══ Robinhood Expansion (docs/robinhood/RH_EXPANSION_CONTRACTS.md §5) ═══════════
-- Added by S1-T0. CREATE … IF NOT EXISTS only — no migrations of existing tables.
-- Decision-class (kept forever): rh_*, kb_priors, politician_*, crowd_*, scanner_hits.
-- Caches (kept forever): option_bars, price_bars_daily.

-- ── RH history (S1) ──
CREATE TABLE IF NOT EXISTS rh_orders (
    id                 TEXT PRIMARY KEY,           -- sha1(account_mask|rh_order_id|leg_idx)[:16]
    account_label      TEXT,
    account_mask       TEXT,
    rh_order_id        TEXT,
    leg_idx            INTEGER DEFAULT 0,
    kind               TEXT,                       -- equity | option | crypto
    symbol             TEXT,
    underlying         TEXT,
    side               TEXT,                       -- buy | sell
    position_effect    TEXT,                       -- open | close | NULL (equity)
    qty                REAL,
    avg_price          REAL,
    fees               REAL,
    filled_at          TEXT,                       -- UTC ISO
    option_type        TEXT,
    strike             REAL,
    expiry             TEXT,
    rh_instrument_id   TEXT,
    is_multileg        INTEGER DEFAULT 0,          -- 1 → excluded from lessons/priors/behavior
    is_drip            INTEGER DEFAULT 0,          -- dividend reinvestment: not a decision
    state              TEXT,
    raw_json           TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rh_orders_acct_filled ON rh_orders(account_label, filled_at);
CREATE INDEX IF NOT EXISTS idx_rh_orders_kind ON rh_orders(kind);

CREATE TABLE IF NOT EXISTS rh_trades (
    id                 TEXT PRIMARY KEY,           -- sha1(account_mask|rh_pnl_row_id)[:16]
    account_label      TEXT,
    account_mask       TEXT,
    kind               TEXT,
    symbol             TEXT,
    underlying         TEXT,
    side               TEXT,
    qty                REAL,
    price              REAL,
    realized_pl        REAL,
    closed_at          TEXT,
    opened_at          TEXT,                       -- FIFO match against rh_orders; NULL if unknown
    holding_days       REAL,
    open_order_ids     TEXT,
    close_order_id     TEXT,
    is_multileg        INTEGER DEFAULT 0,
    option_type        TEXT,
    strike             REAL,
    expiry             TEXT,
    verdict            TEXT,                       -- right | wrong | luck | timing | NULL (unreviewed)
    thesis_text        TEXT,
    verdict_note       TEXT,
    thesis_id          TEXT,
    lesson_id          TEXT,
    story_status       TEXT DEFAULT 'none',        -- none | requested | answered | skipped
    raw_json           TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rh_trades_kind ON rh_trades(kind);

CREATE TABLE IF NOT EXISTS rh_account_snaps (
    account_label      TEXT,
    snap_date          TEXT,
    equity             REAL,
    cash               REAL,
    buying_power       REAL,
    positions_value    REAL,
    raw_json           TEXT,
    created_date       TEXT,
    created_by         TEXT,
    PRIMARY KEY (account_label, snap_date)      -- daily from go-live (forward only)
);

CREATE TABLE IF NOT EXISTS rh_alert_log (
    id                 TEXT PRIMARY KEY,
    rh_alert_id        TEXT,
    symbol             TEXT,
    condition          TEXT,
    level              REAL,
    fired_at           TEXT,
    price_at_fire      REAL,
    raw_json           TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);

-- ── Shared price caches (S1) ──
CREATE TABLE IF NOT EXISTS price_bars_daily (
    ticker             TEXT,
    date               TEXT,
    open               REAL,
    high               REAL,
    low                REAL,
    close              REAL,
    adj_close          REAL,
    volume             REAL,
    source             TEXT,                       -- rh | yfinance
    xcheck_status      TEXT,                       -- ok | mismatch | single_source | not_found
    xcheck_json        TEXT,
    created_date       TEXT,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_price_bars_daily_ticker ON price_bars_daily(ticker);

CREATE TABLE IF NOT EXISTS option_contracts (
    occ                TEXT PRIMARY KEY,           -- OCC symbol e.g. 'NVDA  261218C00200000'
    rh_instrument_id   TEXT,
    underlying         TEXT,
    option_type        TEXT,
    strike             REAL,
    expiry             TEXT,
    resolved_via       TEXT,
    created_date       TEXT
);

CREATE TABLE IF NOT EXISTS option_bars (
    rh_instrument_id   TEXT,
    ts                 INTEGER,
    interval           TEXT,
    open               REAL,
    high               REAL,
    low                REAL,
    close              REAL,
    volume             REAL,
    interpolated       INTEGER,
    created_date       TEXT,
    PRIMARY KEY (rh_instrument_id, interval, ts)
);

CREATE TABLE IF NOT EXISTS trade_excursions (
    id                 TEXT PRIMARY KEY,
    ref_table          TEXT,                       -- options_trades | recommendations | rh_trades
    ref_id             TEXT,
    occ                TEXT,
    entry_ts           TEXT,
    entry_px           REAL,
    exit_ts            TEXT,
    exit_px            REAL,
    mfe_pct            REAL,
    mfe_ts             TEXT,
    mae_pct            REAL,
    mae_ts             TEXT,
    realized_pct       REAL,
    left_on_table_pct  REAL,                       -- mfe_pct - realized_pct
    settle_px          REAL,
    settle_source      TEXT,                       -- alpaca | option_bars | intrinsic
    bars_used          INTEGER,
    interpolated_share REAL,
    grade_json         TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);

-- ── Knowledge (S1 creates, S1+S2 write through mason_kb_priors only) ──
CREATE TABLE IF NOT EXISTS kb_priors (
    id                 TEXT PRIMARY KEY,           -- sha1(family|key_json|window)[:16]; recompute = upsert
    family             TEXT,                       -- personal | behavior | pead | politician | crowd | regime | excursion | scanner
    key_json           TEXT,
    window             TEXT,
    n                  INTEGER,
    stat_json          TEXT,                       -- {mean, median, hit_rate, ci95:[lo,hi], excess_vs_spy, shrunk_mean}
    summary            TEXT,
    min_n_met          INTEGER,
    computed_date      TEXT,
    source_refs        TEXT,
    version            INTEGER DEFAULT 1,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_priors_family ON kb_priors(family);

CREATE TABLE IF NOT EXISTS earnings_results (
    ticker             TEXT,
    fiscal_period      TEXT,
    report_date        TEXT,
    timing             TEXT,
    eps_est            REAL,
    eps_act            REAL,
    surprise_pct       REAL,
    verified           INTEGER,
    raw_json           TEXT,
    created_date       TEXT,
    PRIMARY KEY (ticker, fiscal_period)
);
CREATE INDEX IF NOT EXISTS idx_earnings_results_ticker ON earnings_results(ticker);

CREATE TABLE IF NOT EXISTS market_regimes (
    date               TEXT PRIMARY KEY,
    trend              TEXT,
    vol_regime         TEXT,
    breadth            TEXT,
    label              TEXT,
    features_json      TEXT,
    created_date       TEXT
);

-- ── Signals & tools (S2) ──
CREATE TABLE IF NOT EXISTS politician_trades (
    id                 TEXT PRIMARY KEY,           -- sha1(politician_norm|ticker|tx_date|tx_type|amount_low)[:16]
    politician         TEXT,
    politician_norm    TEXT,
    chamber            TEXT,
    party              TEXT,
    ticker             TEXT,
    tx_type            TEXT,                       -- buy | sell | exchange
    amount_low         REAL,
    amount_high        REAL,
    tx_date            TEXT,
    disclosure_date    TEXT,
    lag_days           INTEGER,
    entry_date         TEXT,                       -- first session close AFTER disclosure_date
    entry_px           REAL,
    fwd_json           TEXT,                       -- {"30":{"ret":..,"xs_spy":..,"matured":bool},"90":..,"180":..}
    raw_json           TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_politician_trades_ticker ON politician_trades(ticker);

CREATE TABLE IF NOT EXISTS crowd_snapshots (
    snap_date          TEXT,
    list_id            TEXT,
    list_name          TEXT,
    ticker             TEXT,
    rank               INTEGER,
    created_date       TEXT,
    PRIMARY KEY (snap_date, list_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_crowd_snapshots_ticker ON crowd_snapshots(ticker);

CREATE TABLE IF NOT EXISTS crowd_events (
    id                 TEXT PRIMARY KEY,
    list_id            TEXT,
    ticker             TEXT,
    kind               TEXT,                       -- enter | exit
    event_date         TEXT,
    px                 REAL,
    fwd_json           TEXT,
    created_date       TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_crowd_events_ticker ON crowd_events(ticker);
CREATE INDEX IF NOT EXISTS idx_crowd_events_kind ON crowd_events(kind);

CREATE TABLE IF NOT EXISTS behavior_findings (
    id                 TEXT PRIMARY KEY,
    metric             TEXT,
    value              REAL,
    baseline           REAL,
    n                  INTEGER,
    ci95_json          TEXT,
    significant        INTEGER,
    finding            TEXT,
    window             TEXT,
    computed_date      TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exit_replays (
    id                 TEXT PRIMARY KEY,
    rh_order_id        TEXT,
    account_label      TEXT,
    ticker             TEXT,
    kind               TEXT,
    sold_at            TEXT,
    sold_px            REAL,
    qty                REAL,
    horizons_json      TEXT,                       -- {"30":{px,dollars,matured},"90":..,"180":..,"today":..}
    dividends_missed   REAL,
    computed_date      TEXT,
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exit_replays_ticker ON exit_replays(ticker);
CREATE INDEX IF NOT EXISTS idx_exit_replays_kind ON exit_replays(kind);

CREATE TABLE IF NOT EXISTS etf_holdings (
    etf                TEXT,
    holding            TEXT,
    weight             REAL,
    as_of              TEXT,
    source             TEXT,                       -- sec_nport | yfinance_top
    created_date       TEXT,
    PRIMARY KEY (etf, holding, source)
);

-- ── Scanners (docs/scanners/SCANNER_INTAKE_DESIGN.md §4 step 3 "Storage") ──
-- Column lists transcribed verbatim from that doc; it gives names only, so types
-- follow this file's conventions (TEXT ids/ISO times, REAL prices, INTEGER counts).
-- Raw Robinhood column cells are kept as JSON so scan column changes need no migration.
CREATE TABLE IF NOT EXISTS scanner_scans (
    scan_id            TEXT PRIMARY KEY,
    title              TEXT,
    columns_json       TEXT,
    sorting            TEXT,
    last_run           TEXT,
    last_error         TEXT,
    created_date       TEXT,                       -- D5 audit (fin 2026-10-20): UTC ISO
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scanner_hits (
    id                 TEXT PRIMARY KEY,
    scan_id            TEXT,
    ticker             TEXT,
    session_date       TEXT,
    first_seen         TEXT,
    last_seen          TEXT,
    exited_at          TEXT,
    reentries          INTEGER DEFAULT 0,
    first_row_json     TEXT,
    peak_row_json      TEXT,
    last_row_json      TEXT,
    first_price        REAL,
    triage_score       REAL,
    triage_note        TEXT,
    triaged_at         TEXT,
    watch_status       TEXT,
    grade_1d           REAL,
    grade_5d           REAL,
    grades_json        TEXT,
    created_date       TEXT,                       -- D5 audit (fin 2026-10-20): UTC ISO
    created_by         TEXT,
    record_deleted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scanner_hits_ticker ON scanner_hits(ticker);

-- ── Learning (Step 5, 2026-11-15; audit L5). Written ONLY by mason_learn.py. ──
-- One row per (item, horizon): what a simple fixed trade on something Mason looked at
-- (catalyst, watch call, dossier, scanner hit, ...) would have returned, next to the
-- S&P 500 over the same days. Feature columns hold what was known when the item existed.
CREATE TABLE IF NOT EXISTS learn_outcomes (
    source_kind          TEXT NOT NULL,            -- catalyst | news_alert | director_watch | director_call | local_watch |
                                                   -- local_call | local_read | dossier | scanner_hit | quick_check
    item_id              TEXT NOT NULL,            -- id of the row in the item's own table
    horizon_days         INTEGER NOT NULL,         -- trading days (1, 3, 5, 10)
    ticker               TEXT,
    item_ts              TEXT,                     -- earliest moment Mason had the item as stored (ISO, Eastern, with offset)
    item_day             TEXT,                     -- Eastern calendar day of item_ts
    direction            TEXT,                     -- long | short | NULL (no direction: raw move only)
    direction_basis      TEXT,                     -- own (the call's direction) | sentiment | none
    label                TEXT,                     -- event type / scan name / call kind
    score                REAL,                     -- catalyst score, materiality or triage score
    confidence           REAL,                     -- the model's stated confidence, when it gave one
    model                TEXT,
    time_of_day          TEXT,
    entry_kind           TEXT,                     -- pre_open | intraday | after_close | non_session
    regime               TEXT,                     -- stored market regime of the last day BEFORE item_day
    sector               TEXT,
    book                 TEXT,                     -- held | watched | NULL (not known)
    days_to_earnings     INTEGER,                  -- calendar days to the next report; negative = days since
    ref_px               REAL,                     -- quote stamped on the item, when it had one
    features_json        TEXT,                     -- keys starting with later_ are what happened AFTERWARDS (status,
                                                   -- traded or not): never inputs. See mason_learn.py's docstring.
    status               TEXT NOT NULL DEFAULT 'pending',   -- pending | graded | not_gradable
    reason               TEXT,                     -- why a row is not gradable
    entry_session        TEXT,
    horizon_session      TEXT,                     -- the session whose close is the exit
    entry_px             REAL,
    entry_rule           TEXT,                     -- minute_0935 | decision_minute | decision_quote | session_open [+next_session]
    entry_exact          INTEGER,                  -- 1 = a price at the right minute; 0 = the session open stood in
    gap_pct              REAL,                     -- entry price against the previous close
    exit_session         TEXT,
    exit_px              REAL,
    hold_sessions        INTEGER,                  -- 0 = bought and sold the same day
    raw_move_pct         REAL,                     -- the stock's move, entry to exit, no cost, no direction
    spy_move_pct         REAL,                     -- the S&P 500 fund over the same window
    raw_minus_spy_pct    REAL,
    cost_pct             REAL,
    result_pct           REAL,                     -- in the item's direction, after cost (NULL without a direction)
    result_minus_spy_pct REAL,                     -- result minus what the S&P 500 did for that direction
    attempts             INTEGER NOT NULL DEFAULT 0,
    created_date         TEXT,
    checked_date         TEXT,
    graded_date          TEXT,
    grader_version       INTEGER,
    -- Dividends (2026-11-16). The price columns above are the price move only and never change;
    -- these say what was paid in cash while held, entry day < ex-date <= exit day, as % of the entry price.
    -- A long holder earns them, a short owes them (the reader applies the direction).
    div_pct              REAL,                     -- the stock's dividends over the window
    spy_div_pct          REAL,                     -- the S&P 500 fund's dividends over the same window
    div_status           TEXT,                     -- counted | none | unknown (no record for one side) | NULL (not looked at)
    PRIMARY KEY (source_kind, item_id, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_learn_outcomes_due ON learn_outcomes(status, horizon_session);
CREATE INDEX IF NOT EXISTS idx_learn_outcomes_ticker ON learn_outcomes(ticker);
"""

_initialized_paths = set()


# ── connection tracker (2026-09-26) ─────────────────────────────────────────────
# The service was found holding 150+ open mason.db connections, one of them sitting
# on an uncommitted write that locked the DB for every writer. Plain sqlite3
# Connections can't be weak-referenced, so connections are created from a trivial
# subclass and registered (weakly — tracking never keeps one alive) with the call
# site that opened it. open_connections() / GET /api/debug/db_connections then
# names exactly who holds what, including which one is inside a write transaction.
import threading as _threading
import traceback as _traceback
import weakref as _weakref


class _TrackedConnection(sqlite3.Connection):
    pass


_OPEN = _weakref.WeakKeyDictionary()      # conn -> {created, thread, site}
_OPEN_LOCK = _threading.Lock()


def _call_site() -> str:
    frames = [f for f in _traceback.extract_stack(limit=10)[:-2]
              if not f.filename.endswith("mason_db.py")]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-4:]))


def with_connection(fn, *args, **kwargs):
    """Call fn(conn, *args, **kwargs) on a fresh connection that is ALWAYS closed
    (for one-shot calls like upsert_rows(get_connection(), ...))."""
    conn = get_connection()
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def open_connections() -> list:
    """Live (not yet closed) connections: age, thread, opener, in_transaction."""
    import time as _t
    out = []
    with _OPEN_LOCK:
        items = list(_OPEN.items())
    for c, meta in items:
        try:
            in_tx = bool(c.in_transaction)       # raises ProgrammingError once closed
        except Exception:
            continue
        out.append({"age_s": round(_t.time() - meta["created"], 1), "thread": meta["thread"],
                    "site": meta["site"], "in_transaction": in_tx})
    out.sort(key=lambda x: (not x["in_transaction"], -x["age_s"]))
    return out


def get_connection() -> sqlite3.Connection:
    """A ready-to-use connection with the schema applied. sqlite3.Row row factory
    so callers can do dict(row). Schema is applied once per distinct DB path."""
    conn = sqlite3.connect(str(DB_FILE), timeout=10, factory=_TrackedConnection)
    conn.row_factory = sqlite3.Row
    try:
        import time as _t
        with _OPEN_LOCK:
            _OPEN[conn] = {"created": _t.time(), "thread": _threading.current_thread().name,
                           "site": _call_site()}
    except Exception:
        pass
    key = str(Path(DB_FILE).resolve())
    if key not in _initialized_paths:
        conn.executescript(_SCHEMA)
        conn.commit()
        # Lightweight in-place migration for columns added after a table already
        # exists on this box (CREATE TABLE IF NOT EXISTS won't add them). Harmless
        # 'duplicate column' error when it's already present.
        try:
            conn.execute("ALTER TABLE reminders ADD COLUMN deletedby TEXT")
            conn.commit()
        except Exception:
            pass
        # reminders edit audit (inline edit in the v2 Reminders widget, 2026-09-27).
        # reminders.kind (2026-09-28): 'note' rows are notes-to-self that stay in the
        # widget but never surface in the greeting/agenda. Existing rows = 'reminder'.
        for _stmt in ("ALTER TABLE reminders ADD COLUMN modifieddate TEXT",
                      "ALTER TABLE reminders ADD COLUMN modifiedby TEXT",
                      "ALTER TABLE reminders ADD COLUMN kind TEXT NOT NULL DEFAULT 'reminder'"):
            try:
                conn.execute(_stmt)
                conn.commit()
            except Exception:
                pass
        # reminders.completed_date (2026-10-23): checked-off reminders are soft-deleted
        # 5 days after completion. Rows already completed when the column lands get
        # stamped "now" (their real check-off time was never stored), so they clear
        # 5 days after this migration rather than all at once.
        try:
            conn.execute("ALTER TABLE reminders ADD COLUMN completed_date TEXT")
            conn.execute("UPDATE reminders SET completed_date = ? "
                         "WHERE completed = 1 AND completed_date IS NULL",
                         (datetime.now().isoformat(),))
            conn.commit()
        except Exception:
            pass
        # news_events Phase-4 columns (added after the table shipped in Phase 1).
        # Audit columns (created_by / deleted_by, and the news soft-delete set)
        # added afterwards — same idempotent ALTER pattern; a 'duplicate column'
        # error just means this box already has them.
        for _stmt in (
            "ALTER TABLE news_events ADD COLUMN entry_price REAL",
            "ALTER TABLE news_events ADD COLUMN calibrated INTEGER DEFAULT 0",
            # created_by / deleted_by audit trail across every table:
            "ALTER TABLE trades ADD COLUMN created_by TEXT",
            "ALTER TABLE trades ADD COLUMN deleted_by TEXT",
            "ALTER TABLE options_trades ADD COLUMN created_by TEXT",
            "ALTER TABLE options_trades ADD COLUMN deleted_by TEXT",
            "ALTER TABLE memory_facts ADD COLUMN created_by TEXT",
            "ALTER TABLE memory_facts ADD COLUMN deleted_by TEXT",
            # news tables gain the full soft-delete audit set:
            "ALTER TABLE news_events ADD COLUMN created_by TEXT DEFAULT 'news_service'",
            "ALTER TABLE news_events ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE news_events ADD COLUMN deleted_at REAL",
            "ALTER TABLE news_events ADD COLUMN deleted_by TEXT",
            "ALTER TABLE news_sources ADD COLUMN created_by TEXT DEFAULT 'news_service'",
            "ALTER TABLE news_sources ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE news_sources ADD COLUMN deleted_at REAL",
            "ALTER TABLE news_sources ADD COLUMN deleted_by TEXT",
            # Catalyst Desk triage columns (2026-09-25) — written by mason_triage:
            "ALTER TABLE news_events ADD COLUMN event_kind TEXT",
            "ALTER TABLE news_events ADD COLUMN market_materiality REAL",
            "ALTER TABLE news_events ADD COLUMN resolved_tickers TEXT",
            "ALTER TABLE news_events ADD COLUMN catalyst_id TEXT",
            "ALTER TABLE news_events ADD COLUMN triaged INTEGER DEFAULT 0",
            # must run AFTER the ALTER above (the column doesn't exist on old DBs at
            # executescript time), so it lives here rather than in _SCHEMA:
            "CREATE INDEX IF NOT EXISTS idx_news_events_catalyst ON news_events(catalyst_id)",
            "CREATE INDEX IF NOT EXISTS idx_news_events_triage ON news_events(triaged, ingested_at DESC)",
            # Activity v2 (2026-09-28): tiers + grouped runs + status
            "ALTER TABLE activity ADD COLUMN tier TEXT",
            "ALTER TABLE activity ADD COLUMN run_id TEXT",
            "ALTER TABLE activity ADD COLUMN status TEXT",
            "CREATE INDEX IF NOT EXISTS idx_activity_tier ON activity(tier, created_date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_activity_run ON activity(run_id)",
            # Scanner audit columns (finalization D5, 2026-10-20). The scanner tables shipped
            # without them; appended here so old DBs end with the same column order as a fresh one.
            "ALTER TABLE scanner_scans ADD COLUMN created_date TEXT",
            "ALTER TABLE scanner_scans ADD COLUMN created_by TEXT",
            "ALTER TABLE scanner_scans ADD COLUMN record_deleted INTEGER DEFAULT 0",
            "ALTER TABLE scanner_hits ADD COLUMN created_date TEXT",
            "ALTER TABLE scanner_hits ADD COLUMN created_by TEXT",
            "ALTER TABLE scanner_hits ADD COLUMN record_deleted INTEGER DEFAULT 0",
            # learn_outcomes dividend columns (2026-11-16), same order as in _SCHEMA. Written only by mason_learn.
            "ALTER TABLE learn_outcomes ADD COLUMN div_pct REAL",
            "ALTER TABLE learn_outcomes ADD COLUMN spy_div_pct REAL",
            "ALTER TABLE learn_outcomes ADD COLUMN div_status TEXT",
        ):
            try:
                conn.execute(_stmt)
                conn.commit()
            except Exception:
                pass
        _initialized_paths.add(key)
    return conn


def reset_schema_cache():
    """Test helper: forget which DB paths have had the schema applied, so a suite
    that repoints DB_FILE at a fresh temp file re-creates the tables there."""
    _initialized_paths.clear()


# ── row <-> dict marshalling ─────────────────────────────────────────────────────

def row_to_dict(row, table: str):
    """A DB row as the plain dict mason_trades / mason_options callers have always
    seen: soft-delete bookkeeping columns removed, JSON columns parsed back to
    dicts, bool columns back to bool, and any extra_json keys merged back in
    (real columns win on a name clash)."""
    if row is None:
        return None
    cfg = TABLES[table]
    d = dict(row)
    extra_raw = d.pop("extra_json", None)
    for c in _SOFT_DELETE_COLS:
        d.pop(c, None)
    for f in cfg["json_fields"]:
        v = d.get(f)
        if isinstance(v, str):
            try:
                d[f] = _json.loads(v)
            except (ValueError, TypeError):
                pass
    for f in cfg["bool_fields"]:
        if d.get(f) is not None:
            d[f] = bool(d[f])
    if extra_raw:
        try:
            for k, v in _json.loads(extra_raw).items():
                d.setdefault(k, v)
        except (ValueError, TypeError):
            pass
    return d


def _row_to_values(row: dict, cfg: dict) -> list:
    """Map a caller dict onto the table's ordered column tuple. Keys with no
    matching column are packed into extra_json instead of being dropped."""
    cols = cfg["columns"]
    colset = set(cols)
    json_fields = cfg["json_fields"]
    bool_fields = cfg["bool_fields"]

    extra = {k: v for k, v in row.items()
             if k not in colset and k not in _INTERNAL_COLS}

    values = []
    for c in cols:
        if c == "extra_json":
            values.append(_json.dumps(extra, default=str) if extra else None)
            continue
        if c == "is_deleted":
            values.append(int(row.get("is_deleted", 0) or 0))
            continue
        if c in ("deleted_at", "deleted_reason", "deleted_by"):
            values.append(row.get(c))
            continue
        v = row.get(c)
        # Backfill the NOT NULL columns when a caller (typically a hand-built
        # test fixture) hands us a partial row. log_trade / log_play /
        # _default_play_row always populate these, so this only ever fires for
        # synthetic rows — it is a safety net, not a code path production hits.
        if not v:
            if c == "created_at":
                v = datetime.now().isoformat()
            elif c == "entry_date":
                v = date.today().isoformat()
            elif c == "status":
                v = "open"
            elif c in ("ticker", "direction", "text"):
                v = ""
            elif c == "tag":
                v = "note"
        if c in json_fields and isinstance(v, (dict, list)):
            v = _json.dumps(v, default=str)
        elif c in bool_fields and v is not None:
            v = int(bool(v))
        values.append(v)
    return values


def upsert_rows(conn: sqlite3.Connection, table: str, rows: list):
    """INSERT each row, or UPDATE every column if its id already exists. One
    transaction. Rows absent from `rows` are left untouched (this is how a
    soft-deleted row stays gone — it is simply never in the active list that
    gets passed back here)."""
    cfg = TABLES[table]
    cols = cfg["columns"]
    collist = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    sql = (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
           f"ON CONFLICT(id) DO UPDATE SET {update_clause}")
    with conn:
        for row in rows:
            conn.execute(sql, _row_to_values(row, cfg))


def soft_delete(conn: sqlite3.Connection, table: str, row_id: str,
                reason: str, deleted_at: str, deleted_by: str = "nico"):
    """Mark one row deleted. It stays in the raw table forever; it just stops
    appearing in the active_* view from here on.

    deleted_by records who retired the row: 'nico' (Nico asked, the default for
    tool-driven deletes), 'mason' (the assistant decided to), or 'system' (a
    maintenance script — dedupe, reconcile, rebuild). It pairs with deleted_at /
    deleted_reason to give every soft delete a complete who/when/why trail.

    2026-09-10 (Nico's rule): a deleted trade/play must never read as 'open' —
    the only rows allowed to be 'open' are positions actually live on Alpaca. So
    when we delete a still-'open' row we also stamp status='closed'. We touch ONLY
    status='open' (leave closed/expired/reviewed as they were), and undelete
    deliberately does NOT resurrect 'open' — a restored row that isn't live on
    Alpaca must not read open either; reconciliation re-opens it if it truly is.
    memory_facts has no status column, so this is scoped to the trade tables."""
    with conn:
        conn.execute(
            f"UPDATE {table} SET is_deleted = 1, deleted_at = ?, deleted_reason = ?, "
            f"deleted_by = ? WHERE id = ?",
            (deleted_at, reason or "no reason given", deleted_by or "nico", row_id),
        )
        if table in ("trades", "options_trades"):
            conn.execute(
                f"UPDATE {table} SET status = 'closed' WHERE id = ? AND status = 'open'",
                (row_id,),
            )


def restore(conn: sqlite3.Connection, table: str, row_id: str) -> bool:
    """Undo a soft delete — the row rejoins the active_* view. Returns True if a
    deleted row was actually restored, False if the id was unknown or already
    live. The deleted_at / deleted_reason / deleted_by are cleared."""
    with conn:
        cur = conn.execute(
            f"UPDATE {table} SET is_deleted = 0, deleted_at = NULL, "
            f"deleted_reason = NULL, deleted_by = NULL WHERE id = ? AND is_deleted = 1",
            (row_id,),
        )
        return cur.rowcount > 0
