-- WDBS minimal synthetic stand-in for an FS42-style schedule database (test use only).
--
-- PROVENANCE: independently authored for this repository from the interface that WDBS code and tests actually use:
--   wdbs/fs42_adapter.py (get_catalog, get_blocks, get_sequences, fingerprints, other_tables_fingerprint),
--   wdbs/replan.py, wdbs/recorder.py, wdbs/prefetch.py, wdbs/player_control.py (SELECT/UPDATE/DELETE column lists),
--   tests/fixture.py (INSERT column lists) and the inline CREATE TABLE statements in tests/test_core.py and
--   tests/test_era_id.py. It was NOT copied, dumped or mechanically paraphrased from any FieldStation42 database or file.
--   Table and column NAMES are fixed by the interface WDBS reads (they are names, not creative expression).
--
-- LIMITATIONS: this is a subset. Real FS42 databases may have more columns, different types/defaults/constraints and
-- indexes; none of that is verified here and no FS42 revision is claimed compatible. Constraints are deliberately
-- loose (no NOT NULL / UNIQUE / foreign keys) so the harness exercises WDBS logic, not FS42 integrity rules.
-- Timestamps are TEXT in 'YYYY-MM-DD HH:MM:SS' (what wdbs.fs42_adapter.TS_FORMAT reads and writes); JSON columns are TEXT.

-- Catalog of playable files. Read by id/path/realpath/title/duration/tag/count/content_type; `count` is the
-- scheduling play-count that replan gives back for deleted blocks.
CREATE TABLE catalog_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT,
    path TEXT,
    realpath TEXT,
    title TEXT,
    duration REAL,
    tag TEXT,
    count INTEGER DEFAULT 0,
    content_type TEXT
);

-- Schedule blocks. content_json holds a catalog id, plan_json the playable segments, sequence_key the
-- sequence name/tag path (all JSON text); times are TEXT so ordering is lexicographic.
CREATE TABLE liquid_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT,
    liquid_type TEXT,
    start_time TEXT,
    end_time TEXT,
    break_strategy TEXT,
    title TEXT,
    sequence_key TEXT,
    break_info TEXT,
    content_json TEXT,
    plan_json TEXT
);
CREATE INDEX idx_min_blocks_station_start ON liquid_blocks (station, start_time);   -- get_blocks() range queries

-- Episode sequences. current_index is the index of the NEXT episode to schedule (replan's compare-and-swap target).
CREATE TABLE named_sequence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT,
    sequence_name TEXT,
    tag_path TEXT,
    start_perc REAL,
    end_perc REAL,
    current_index INTEGER,
    initialized INTEGER,
    parent_tag TEXT
);

CREATE TABLE sequence_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fpath TEXT,
    sequence_index INTEGER,
    named_sequence_id INTEGER
);
CREATE INDEX idx_min_seq_entries ON sequence_entries (named_sequence_id, sequence_index);   -- get_sequences() ordering

-- Tables WDBS only counts or fingerprints (replan checks they are unchanged); prefetch reads file_meta.duration.
CREATE TABLE sequence_group_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station TEXT
);
CREATE TABLE file_meta (
    path TEXT PRIMARY KEY,
    duration REAL
);
CREATE TABLE break_points (
    path TEXT PRIMARY KEY,
    points TEXT
);
CREATE TABLE chapter_points (
    path TEXT PRIMARY KEY,
    points TEXT
);
