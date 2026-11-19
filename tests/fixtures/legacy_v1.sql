BEGIN TRANSACTION;
CREATE TABLE acceptance_profiles(
      profile_hash TEXT PRIMARY KEY, content_json TEXT NOT NULL, created_at TEXT NOT NULL);
INSERT INTO "acceptance_profiles" VALUES('8b21c4070f9d4c0681c30f4b0f15cb88e504b83bd8b4877f0ec8ac8978321dbd','{"allowed_paths":[],"commands":[],"max_log_bytes":10485760,"requires_gpu":false,"schema":1}','2026-11-19T15:22:48Z');
CREATE TABLE approvals(
      approval_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, kind TEXT NOT NULL,
      target_commit TEXT NOT NULL, target_tree TEXT NOT NULL, decision_id TEXT,
      integration_id TEXT, granted_via TEXT NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT);
CREATE TABLE claims(
      claim_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds, actor TEXT NOT NULL,
      kind TEXT NOT NULL, client_request_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      payload_hash TEXT NOT NULL, fingerprint TEXT, status TEXT NOT NULL, duplicate_of TEXT,
      reject_reason TEXT, created_at TEXT NOT NULL,
      UNIQUE(round_id, actor, kind, client_request_id));
CREATE TABLE events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, entity_type TEXT NOT NULL,
      entity_id TEXT NOT NULL, from_state TEXT, to_state TEXT, actor TEXT, detail_json TEXT);
INSERT INTO "events" VALUES(1,'2026-11-19T15:22:48Z','repo','toy',NULL,'REGISTERED','user','{"path":"fixture-root\\repo","trusted_for_exec":false}');
INSERT INTO "events" VALUES(2,'2026-11-19T15:22:48Z','repo','toy',NULL,NULL,'user','{"policy_hash":"6c74275d2519c70e8e21d1e054f75864fc7575034c337f9a9805fcf7c3425d1a"}');
INSERT INTO "events" VALUES(3,'2026-11-19T15:22:48Z','task','T20261019-migrate',NULL,'CREATED','user','{"frozen_policy":{"frozen":[{"note":"M0a fixture","path":"src/frozen.py","ref":"review","symbols":["f"]}],"schema":1},"policy_hash":"6c74275d2519c70e8e21d1e054f75864fc7575034c337f9a9805fcf7c3425d1a","repo_id":"toy"}');
CREATE TABLE evidence(
      evidence_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds, kind TEXT NOT NULL,
      status TEXT NOT NULL, commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL,
      profile_hash TEXT NOT NULL, results_json TEXT, env_json TEXT, producer TEXT NOT NULL,
      created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE integrations(
      integration_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks,
      candidate_commit TEXT NOT NULL, candidate_tree TEXT NOT NULL, main_before TEXT NOT NULL,
      state TEXT NOT NULL, check_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('schema_version','1');
CREATE TABLE operations(
      op_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos, kind TEXT NOT NULL,
      ref TEXT, expected_old TEXT, target TEXT, status TEXT NOT NULL,
      idempotency_id TEXT NOT NULL UNIQUE, on_applied_json TEXT, detail_json TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE repos(
      repo_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
      main_ref TEXT NOT NULL DEFAULT 'refs/heads/main', worktree_root TEXT,
      trusted_for_exec INTEGER NOT NULL DEFAULT 0, policy_json TEXT, policy_hash TEXT,
      created_at TEXT NOT NULL);
INSERT INTO "repos" VALUES('toy','fixture-root\repo','refs/heads/main',NULL,0,'{"frozen":[{"note":"M0a fixture","path":"src/frozen.py","ref":"review","symbols":["f"]}],"schema":1}','6c74275d2519c70e8e21d1e054f75864fc7575034c337f9a9805fcf7c3425d1a','2026-11-19T15:22:48Z');
CREATE TABLE reviews(
      review_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks,
      round_id TEXT NOT NULL REFERENCES rounds, reviewer TEXT NOT NULL, state TEXT NOT NULL,
      verdict TEXT, independence TEXT, base_commit TEXT NOT NULL, reviewed_commit TEXT NOT NULL,
      reviewed_tree TEXT NOT NULL, scope_id TEXT NOT NULL, acceptance_profile_hash TEXT NOT NULL,
      findings_json TEXT, claim_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE rounds(
      round_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, n INTEGER NOT NULL,
      actor TEXT NOT NULL, role TEXT NOT NULL, base_commit TEXT NOT NULL, state TEXT NOT NULL,
      head_commit TEXT, head_tree TEXT, branch TEXT, worktree_path TEXT, claim_id TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(task_id, n));
CREATE TABLE tasks(
      task_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos,
      goal TEXT NOT NULL, state TEXT NOT NULL, base_commit TEXT NOT NULL,
      profile_hash TEXT NOT NULL REFERENCES acceptance_profiles, policy_hash TEXT,
      source TEXT NOT NULL DEFAULT 'native', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO "tasks" VALUES('T20261019-migrate','toy','M0a migration fixture','CREATED','016e448d6f1b5e5c5815686973915555fe191158','8b21c4070f9d4c0681c30f4b0f15cb88e504b83bd8b4877f0ec8ac8978321dbd','6c74275d2519c70e8e21d1e054f75864fc7575034c337f9a9805fcf7c3425d1a','native','2026-11-19T15:22:48Z','2026-11-19T15:22:48Z');
CREATE INDEX ix_rounds_task ON rounds(task_id, n);
CREATE INDEX ix_reviews_round ON reviews(round_id);
CREATE INDEX ix_events_entity ON events(entity_type, entity_id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',3);
COMMIT;
