
_LEDGER_COLS = [
    "id", "person_id", "entry_type", "amount_kobo", "agreement_id", "target_entry_id",
    "occurred_at", "recorded_at", "override_over_kobo", "source_action_id", "evidence_id",
]


def _immutable_cond(cols):
    return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in cols)


SCHEMA_SQL = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE policy (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  per_payment_limit_kobo INTEGER NOT NULL CHECK (per_payment_limit_kobo > 0),
  recurring_min_count INTEGER NOT NULL CHECK (recurring_min_count >= 1),
  duplicate_window_hours INTEGER NOT NULL CHECK (duplicate_window_hours >= 1),
  draft_ttl_seconds INTEGER NOT NULL CHECK (draft_ttl_seconds >= 1),
  version INTEGER NOT NULL
);

CREATE TABLE person (
  id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('supplier','customer','other')),
  payee_status TEXT NOT NULL DEFAULT 'not_approved' CHECK (payee_status IN ('approved','not_approved'))
);
CREATE TRIGGER tr_person_no_delete BEFORE DELETE ON person
BEGIN SELECT RAISE(ABORT, 'person_no_delete'); END;
CREATE TRIGGER tr_person_update BEFORE UPDATE ON person
WHEN NEW.id IS NOT OLD.id OR NEW.legal_name IS NOT OLD.legal_name OR NEW.role IS NOT OLD.role
BEGIN SELECT RAISE(ABORT, 'person_immutable'); END;

CREATE TABLE person_alias (
  person_id TEXT NOT NULL REFERENCES person(id),
  alias_normalized TEXT NOT NULL,
  UNIQUE (person_id, alias_normalized)
);

CREATE TABLE evidence (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL CHECK (source_type IN ('manual_entry','pasted_message','csv_import')),
  raw_text TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TRIGGER tr_evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence_immutable'); END;
CREATE TRIGGER tr_evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence_immutable'); END;

CREATE TABLE agreement (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES person(id),
  description TEXT NOT NULL,
  total_kobo INTEGER NOT NULL CHECK (typeof(total_kobo) = 'integer' AND total_kobo > 0),
  agreed_at TEXT NOT NULL,
  valid_until TEXT,
  supersedes_id TEXT REFERENCES agreement(id),
  evidence_id TEXT NOT NULL REFERENCES evidence(id)
);
CREATE UNIQUE INDEX ux_agreement_supersedes ON agreement(supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE TRIGGER tr_agreement_insert BEFORE INSERT ON agreement
WHEN NEW.supersedes_id IS NOT NULL
BEGIN
  SELECT CASE
    WHEN (SELECT person_id FROM agreement WHERE id = NEW.supersedes_id) IS NOT NEW.person_id
      THEN RAISE(ABORT, 'supersede_person_mismatch')
    WHEN NEW.agreed_at < (SELECT agreed_at FROM agreement WHERE id = NEW.supersedes_id)
      THEN RAISE(ABORT, 'supersede_before_predecessor')
  END;
END;
CREATE TRIGGER tr_agreement_no_update BEFORE UPDATE ON agreement
BEGIN SELECT RAISE(ABORT, 'agreement_immutable'); END;
CREATE TRIGGER tr_agreement_no_delete BEFORE DELETE ON agreement
BEGIN SELECT RAISE(ABORT, 'agreement_immutable'); END;

-- Staged action before ledger_entry because ledger_entry.source_action_id references it.
CREATE TABLE instruction (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  raw_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  parsed_intent_json TEXT,
  parse_valid INTEGER NOT NULL DEFAULT 0 CHECK (parse_valid IN (0,1)),
  model_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('decided','waiting','cancelled')),
  resolved_intent_json TEXT NOT NULL
);

CREATE TABLE decision (
  id TEXT PRIMARY KEY,
  instruction_id TEXT NOT NULL REFERENCES instruction(id),
  seq INTEGER NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('STAGE','ASK','REFUSE','WAIT')),
  reason_code TEXT,
  rule_version TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (instruction_id, seq)
);
CREATE TRIGGER tr_decision_no_update BEFORE UPDATE ON decision
BEGIN SELECT RAISE(ABORT, 'decision_immutable'); END;
CREATE TRIGGER tr_decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decision_immutable'); END;

CREATE TABLE staged_action (
  id TEXT PRIMARY KEY,
  instruction_id TEXT NOT NULL REFERENCES instruction(id),
  decision_id TEXT NOT NULL REFERENCES decision(id),
  action_type TEXT NOT NULL CHECK (action_type = 'PAYMENT'),
  recipient_person_id TEXT NOT NULL REFERENCES person(id),
  amount_kobo INTEGER NOT NULL CHECK (typeof(amount_kobo) = 'integer' AND amount_kobo > 0),
  currency TEXT NOT NULL CHECK (currency = 'NGN'),
  source TEXT NOT NULL CHECK (source IN ('explicit','outstanding_balance','recurring_pattern')),
  agreement_id TEXT REFERENCES agreement(id),
  stated_amount_kobo INTEGER,
  amount_resolution_json TEXT NOT NULL,
  ack_duplicate_json TEXT NOT NULL,
  ack_unlinked_json TEXT NOT NULL,
  override_over_kobo INTEGER CHECK (override_over_kobo IS NULL OR override_over_kobo > 0),
  purpose_text TEXT NOT NULL,
  computation_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  resolved_intent_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  read_set_digest TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('STAGED','COMMITTED','CANCELLED','EXPIRED','INVALIDATED')),
  state_reason TEXT,
  finalized_at TEXT,
  ledger_entry_id TEXT
);
-- At most one live draft per payload.
CREATE UNIQUE INDEX ux_staged_payload_live ON staged_action(payload_hash) WHERE state = 'STAGED';
CREATE TRIGGER tr_staged_insert BEFORE INSERT ON staged_action
BEGIN
  SELECT CASE
    WHEN NEW.state <> 'STAGED' THEN RAISE(ABORT, 'staged_must_start_staged')
    WHEN (SELECT payee_status FROM person WHERE id = NEW.recipient_person_id) IS NOT 'approved'
      THEN RAISE(ABORT, 'payee_not_approved')
    WHEN NEW.amount_kobo > (SELECT per_payment_limit_kobo FROM policy WHERE id = 1)
      THEN RAISE(ABORT, 'over_limit')
  END;
END;
CREATE TRIGGER tr_staged_update BEFORE UPDATE ON staged_action
BEGIN
  SELECT CASE
    WHEN OLD.state <> 'STAGED' THEN RAISE(ABORT, 'terminal_state')
    WHEN NEW.state NOT IN ('COMMITTED','CANCELLED','EXPIRED','INVALIDATED') THEN RAISE(ABORT, 'bad_transition')
    WHEN NEW.id IS NOT OLD.id OR NEW.instruction_id IS NOT OLD.instruction_id
      OR NEW.decision_id IS NOT OLD.decision_id OR NEW.recipient_person_id IS NOT OLD.recipient_person_id
      OR NEW.amount_kobo IS NOT OLD.amount_kobo OR NEW.currency IS NOT OLD.currency
      OR NEW.source IS NOT OLD.source OR NEW.agreement_id IS NOT OLD.agreement_id
      OR NEW.stated_amount_kobo IS NOT OLD.stated_amount_kobo
      OR NEW.amount_resolution_json IS NOT OLD.amount_resolution_json
      OR NEW.ack_duplicate_json IS NOT OLD.ack_duplicate_json
      OR NEW.ack_unlinked_json IS NOT OLD.ack_unlinked_json
      OR NEW.override_over_kobo IS NOT OLD.override_over_kobo
      OR NEW.purpose_text IS NOT OLD.purpose_text
      OR NEW.computation_json IS NOT OLD.computation_json
      OR NEW.evidence_json IS NOT OLD.evidence_json
      OR NEW.resolved_intent_json IS NOT OLD.resolved_intent_json
      OR NEW.payload_hash IS NOT OLD.payload_hash
      OR NEW.read_set_digest IS NOT OLD.read_set_digest
      OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
      THEN RAISE(ABORT, 'staged_immutable')
  END;
END;
CREATE TRIGGER tr_staged_no_delete BEFORE DELETE ON staged_action
BEGIN SELECT RAISE(ABORT, 'staged_no_delete'); END;

CREATE TABLE action_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id TEXT NOT NULL REFERENCES staged_action(id),
  seq INTEGER NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT,
  at TEXT NOT NULL,
  UNIQUE (action_id, seq)
);
CREATE TRIGGER tr_event_no_update BEFORE UPDATE ON action_event
BEGIN SELECT RAISE(ABORT, 'event_immutable'); END;
CREATE TRIGGER tr_event_no_delete BEFORE DELETE ON action_event
BEGIN SELECT RAISE(ABORT, 'event_immutable'); END;

CREATE TABLE commit_attempt (
  commit_key TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES staged_action(id),
  payload_hash TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER tr_commit_attempt_no_update BEFORE UPDATE ON commit_attempt
BEGIN SELECT RAISE(ABORT, 'commit_attempt_immutable'); END;
CREATE TRIGGER tr_commit_attempt_no_delete BEFORE DELETE ON commit_attempt
BEGIN SELECT RAISE(ABORT, 'commit_attempt_immutable'); END;

CREATE TABLE ledger_entry (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL REFERENCES person(id),
  entry_type TEXT NOT NULL CHECK (entry_type IN ('payment_out','refund_out','payment_in','reversal','reinstatement')),
  amount_kobo INTEGER NOT NULL CHECK (typeof(amount_kobo) = 'integer' AND amount_kobo > 0),
  agreement_id TEXT REFERENCES agreement(id),
  target_entry_id TEXT REFERENCES ledger_entry(id),
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  reconciliation TEXT NOT NULL DEFAULT 'reconciled' CHECK (reconciliation IN ('reconciled','pending','disputed')),
  override_over_kobo INTEGER CHECK (override_over_kobo IS NULL OR override_over_kobo > 0),
  source_action_id TEXT UNIQUE REFERENCES staged_action(id),
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  CHECK ((entry_type IN ('reversal','reinstatement')) = (target_entry_id IS NOT NULL)),
  CHECK (override_over_kobo IS NULL OR entry_type = 'payment_out'),
  CHECK (source_action_id IS NULL OR (entry_type = 'payment_out' AND reconciliation = 'reconciled'))
);
CREATE UNIQUE INDEX ux_adjustment_evidence ON ledger_entry(evidence_id)
  WHERE entry_type IN ('reversal','reinstatement');
CREATE INDEX ix_ledger_person ON ledger_entry(person_id, occurred_at);
CREATE INDEX ix_ledger_target ON ledger_entry(target_entry_id);

-- TR5: append-only. With recursive_triggers=ON this also fires for INSERT OR REPLACE.
CREATE TRIGGER tr_ledger_no_delete BEFORE DELETE ON ledger_entry
BEGIN SELECT RAISE(ABORT, 'ledger_append_only'); END;

-- TR4: only reconciliation may change, and only along pending->reconciled, pending->disputed,
-- disputed->reconciled.
CREATE TRIGGER tr_ledger_update BEFORE UPDATE ON ledger_entry
BEGIN
  SELECT CASE
    WHEN {_immutable_cond(_LEDGER_COLS)} THEN RAISE(ABORT, 'ledger_immutable')
    WHEN NOT (
      (OLD.reconciliation = 'pending' AND NEW.reconciliation IN ('reconciled','disputed'))
      OR (OLD.reconciliation = 'disputed' AND NEW.reconciliation = 'reconciled')
    ) THEN RAISE(ABORT, 'bad_reconciliation_transition')
  END;
END;

CREATE TRIGGER tr_ledger_agreement_person BEFORE INSERT ON ledger_entry
WHEN NEW.agreement_id IS NOT NULL
BEGIN
  SELECT CASE
    WHEN (SELECT person_id FROM agreement WHERE id = NEW.agreement_id) IS NOT NEW.person_id
      THEN RAISE(ABORT, 'agreement_person_mismatch')
  END;
END;

-- TR1: reversal/reinstatement shape.
CREATE TRIGGER tr_ledger_adjustment_shape BEFORE INSERT ON ledger_entry
WHEN NEW.entry_type IN ('reversal','reinstatement')
BEGIN
  SELECT CASE
    WHEN (SELECT id FROM ledger_entry WHERE id = NEW.target_entry_id) IS NULL
      THEN RAISE(ABORT, 'target_missing')
    WHEN (SELECT person_id FROM ledger_entry WHERE id = NEW.target_entry_id) IS NOT NEW.person_id
      THEN RAISE(ABORT, 'target_person_mismatch')
    WHEN (SELECT agreement_id FROM ledger_entry WHERE id = NEW.target_entry_id) IS NOT NEW.agreement_id
      THEN RAISE(ABORT, 'target_agreement_mismatch')
    WHEN NEW.occurred_at < (SELECT occurred_at FROM ledger_entry WHERE id = NEW.target_entry_id)
      THEN RAISE(ABORT, 'adjustment_before_target')
    WHEN (SELECT reconciliation FROM ledger_entry WHERE id = NEW.target_entry_id) <> 'reconciled'
      THEN RAISE(ABORT, 'target_not_reconciled')
    WHEN NEW.entry_type = 'reversal'
      AND (SELECT entry_type FROM ledger_entry WHERE id = NEW.target_entry_id) <> 'payment_out'
      THEN RAISE(ABORT, 'bad_target_type')
    WHEN NEW.entry_type = 'reinstatement'
      AND (SELECT entry_type FROM ledger_entry WHERE id = NEW.target_entry_id) <> 'reversal'
      THEN RAISE(ABORT, 'bad_target_type')
  END;
END;

-- TR2: sum(all reversals) - sum(reconciled reinstatements of them) + NEW <= payment amount.
CREATE TRIGGER tr_ledger_reversal_cap BEFORE INSERT ON ledger_entry
WHEN NEW.entry_type = 'reversal'
BEGIN
  SELECT CASE
    WHEN (
      (SELECT COALESCE(SUM(amount_kobo), 0) FROM ledger_entry
         WHERE entry_type = 'reversal' AND target_entry_id = NEW.target_entry_id)
      - (SELECT COALESCE(SUM(i.amount_kobo), 0) FROM ledger_entry i
           JOIN ledger_entry r ON i.target_entry_id = r.id
           WHERE i.entry_type = 'reinstatement' AND i.reconciliation = 'reconciled'
             AND r.target_entry_id = NEW.target_entry_id)
      + NEW.amount_kobo
    ) > (SELECT amount_kobo FROM ledger_entry WHERE id = NEW.target_entry_id)
    THEN RAISE(ABORT, 'reversal_cap_exceeded')
  END;
END;

-- TR3: sum(all reinstatements of a reversal) + NEW <= that reversal's amount.
CREATE TRIGGER tr_ledger_reinstatement_cap BEFORE INSERT ON ledger_entry
WHEN NEW.entry_type = 'reinstatement'
BEGIN
  SELECT CASE
    WHEN (
      (SELECT COALESCE(SUM(amount_kobo), 0) FROM ledger_entry
         WHERE entry_type = 'reinstatement' AND target_entry_id = NEW.target_entry_id)
      + NEW.amount_kobo
    ) > (SELECT amount_kobo FROM ledger_entry WHERE id = NEW.target_entry_id)
    THEN RAISE(ABORT, 'reinstatement_cap_exceeded')
  END;
END;

-- TR6: payee approval and limit backstop for agent-originated payments.
CREATE TRIGGER tr_ledger_payee BEFORE INSERT ON ledger_entry
WHEN NEW.source_action_id IS NOT NULL
BEGIN
  SELECT CASE
    WHEN (SELECT payee_status FROM person WHERE id = NEW.person_id) IS NOT 'approved'
      THEN RAISE(ABORT, 'payee_not_approved')
    WHEN NEW.amount_kobo > (SELECT per_payment_limit_kobo FROM policy WHERE id = 1)
      THEN RAISE(ABORT, 'over_limit')
  END;
END;
"""
