"""
Database schema (PLAN.md, section 4) as numbered migrations.

Each migration is a list of single SQL statements, applied in order inside one
``BEGIN IMMEDIATE`` transaction together with the ``schema_version`` bump. Statements are
executed one at a time: ``sqlite3.Connection.executescript`` would commit the open
transaction first and break the atomicity this relies on.

Times are integer milliseconds since the epoch (see ``app.store.db.now_ms``).

Never edit a migration that has shipped; append a new one.
"""

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            # Accepted catalog definitions. Authoritative: everything reads this table, never
            # the YAML files (PLAN.md section 3). epoch is bumped by every accepted revision and
            # every maintenance operation; a run must see the same epoch at commit.
            """
            CREATE TABLE catalog_defs (
                catalog_id      TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                config_rev      TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                state           TEXT NOT NULL CHECK (state IN ('enabled', 'disabled', 'removed')),
                position        INTEGER NOT NULL DEFAULT 0,
                epoch           INTEGER NOT NULL DEFAULT 0,
                accepted_at     INTEGER NOT NULL,
                last_error      TEXT
            )
            """,
            """
            CREATE TABLE runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id              INTEGER REFERENCES jobs(id),
                catalog_id          TEXT NOT NULL REFERENCES catalog_defs(catalog_id),
                config_rev          TEXT NOT NULL,
                epoch               INTEGER NOT NULL,
                seed                TEXT,
                trigger             TEXT NOT NULL,
                dry_run             INTEGER NOT NULL DEFAULT 0,
                started_at          INTEGER NOT NULL,
                finished_at         INTEGER,
                status              TEXT NOT NULL
                                    CHECK (status IN ('running', 'ok', 'failed', 'superseded', 'abandoned')),
                stage_counts_json   TEXT,
                reject_reasons_json TEXT,
                prompt_hash         TEXT,
                model_calls         INTEGER NOT NULL DEFAULT 0,
                tokens_in           INTEGER NOT NULL DEFAULT 0,
                tokens_out          INTEGER NOT NULL DEFAULT 0,
                relaxed_window      INTEGER,
                repeats             INTEGER NOT NULL DEFAULT 0,
                -- A controlled value, never provider text (which can quote keys).
                error_category      TEXT,
                error_status        INTEGER
            )
            """,
            # metas_json is set to NULL when the payload is pruned; the row itself stays
            # because activations reference it.
            """
            CREATE TABLE snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_id  TEXT NOT NULL REFERENCES catalog_defs(catalog_id),
                created_at  INTEGER NOT NULL,
                run_id      INTEGER REFERENCES runs(id),
                config_rev  TEXT NOT NULL,
                status      TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
                item_count  INTEGER NOT NULL,
                metas_json  TEXT
            )
            """,
            "CREATE UNIQUE INDEX snapshots_one_active ON snapshots(catalog_id) WHERE status = 'active'",
            "CREATE INDEX snapshots_by_catalog ON snapshots(catalog_id, id)",
            # One row per publication. id order is activation order. Never pruned.
            """
            CREATE TABLE activations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_id   TEXT NOT NULL REFERENCES catalog_defs(catalog_id),
                snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
                run_id       INTEGER REFERENCES runs(id),
                activated_at INTEGER NOT NULL,
                revoked_at   INTEGER
            )
            """,
            "CREATE INDEX activations_by_catalog ON activations(catalog_id, id)",
            # One row per item per activation. Never pruned.
            """
            CREATE TABLE history (
                activation_id INTEGER NOT NULL REFERENCES activations(id),
                catalog_id    TEXT NOT NULL,
                tmdb_id       INTEGER,
                imdb_id       TEXT NOT NULL,
                title         TEXT NOT NULL,
                year          INTEGER
            )
            """,
            "CREATE INDEX history_by_activation ON history(activation_id)",
            "CREATE INDEX history_by_catalog_imdb ON history(catalog_id, imdb_id)",
            "CREATE INDEX history_by_catalog_tmdb ON history(catalog_id, tmdb_id)",
            """
            CREATE TABLE jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                catalog_id   TEXT NOT NULL REFERENCES catalog_defs(catalog_id),
                kind         TEXT NOT NULL CHECK (kind IN ('refresh', 'dry_run')),
                trigger      TEXT NOT NULL,
                state        TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed')),
                state_detail TEXT,
                requested_at INTEGER NOT NULL,
                not_before   INTEGER NOT NULL,
                attempt      INTEGER NOT NULL DEFAULT 0,
                claimed_by   TEXT,
                claim_gen    INTEGER,
                heartbeat_at INTEGER
            )
            """,
            "CREATE UNIQUE INDEX jobs_one_open ON jobs(catalog_id) WHERE state IN ('pending', 'running')",
            "CREATE INDEX jobs_pending ON jobs(state, not_before, requested_at)",
            """
            CREATE TABLE schedule (
                catalog_id  TEXT PRIMARY KEY REFERENCES catalog_defs(catalog_id),
                next_run_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE lease (
                name       TEXT PRIMARY KEY,
                owner      TEXT,
                generation INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL DEFAULT 0
            )
            """,
            "INSERT INTO lease (name, owner, generation, expires_at) VALUES ('worker', NULL, 0, 0)",
            # One row per outbound model attempt, written before the call (reservation).
            """
            CREATE TABLE llm_ledger (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                day        TEXT NOT NULL,
                catalog_id TEXT,
                run_id     INTEGER REFERENCES runs(id),
                at         INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                model      TEXT NOT NULL,
                status     TEXT NOT NULL CHECK (status IN ('reserved', 'done', 'failed')),
                tokens_in  INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX llm_ledger_by_day ON llm_ledger(day)",
            """
            CREATE TABLE tmdb_cache (
                key          TEXT PRIMARY KEY,
                tmdb_id      INTEGER,
                details_json TEXT,
                fetched_at   INTEGER NOT NULL
            )
            """,
        ],
    ),
    (
        2,
        [
            # The structured-output mode that last worked per (base URL, model), so a downgrade
            # found by one process (worker, CLI, dry run) is not rediscovered at the budget's
            # expense by the next (PLAN.md section 7). Rows older than 24 h are ignored.
            """
            CREATE TABLE llm_modes (
                key  TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK (mode IN ('json_schema', 'json_object', 'text')),
                at   INTEGER NOT NULL
            )
            """,
            "CREATE INDEX llm_ledger_by_run ON llm_ledger(run_id)",
        ],
    ),
    (
        3,
        [
            # Exclusion names resolved to TMDB ids when a definition is applied. Kept out of
            # definition_json so TMDB drift (votes, new remakes) never changes config_rev or
            # bumps the epoch. JSON: {"inputs_rev", "blacklist_tmdb_ids", "collection_ids",
            # "company_ids", "keyword_ids", "title_patterns", "unresolved"}.
            "ALTER TABLE catalog_defs ADD COLUMN exclusions_json TEXT",
            # 'file' for definitions applied from catalogs.d (only those are marked removed
            # when their file disappears); 'fixture' for load-fixture and test catalogs.
            "ALTER TABLE catalog_defs ADD COLUMN source TEXT NOT NULL DEFAULT 'fixture'",
            # Definition files that failed validation, by catalog id (the file name stem), so
            # a new file that never validated still has its error on record. Cleared when the
            # file validates or disappears.
            """
            CREATE TABLE definition_errors (
                catalog_id TEXT PRIMARY KEY,
                file       TEXT NOT NULL,
                error      TEXT NOT NULL,
                at         INTEGER NOT NULL
            )
            """,
        ],
    ),
    (
        4,
        [
            # The owner's watched films from MDBList (app.curation.watched), shared by every
            # catalog with exclude_watched. One row per source, replaced only after a complete
            # fetch; read while younger than its TTL (6 h). JSON: {"tmdb_ids", "imdb_ids",
            # "titles": [[title, year], ...] most recently watched first}. No key material.
            """
            CREATE TABLE watched_cache (
                key          TEXT PRIMARY KEY,
                fetched_at   INTEGER NOT NULL,
                item_count   INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
        ],
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]
