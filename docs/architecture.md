# Orda architecture (v0.1, CLI-core MVP)

Orda owns only a routing projection + delivery ledger. Canonical work state
lives in Eldunari (`~/.hermes/eldunari/nexus/state/orda/`); this package
never forks it.

## Module map

- `courier/intake.py` — raw message -> canonical Message (deterministic, no
  model, no payload telemetry).
- `courier/redact.py` — secret redaction BEFORE any router-model call;
  deny-by-default classes from explicit config; strict mode blocks.
- `courier/topics.py` — layered topic candidates: explicit slug >
  metadata/keyword over slugs + <=500-char summaries; never transcripts.
- `courier/router.py` — redacted Message + candidates -> RouteDecision, one of
  CONTINUE | NEW | DIRECT | HANDOFF | ESCALATE; `RouterModel` interface +
  fixed escalation ladder (router-t1 -> retry -> main-model ->
  clarification), every step recorded.
- `courier/policy.py` — tier precedence local-configured > free (gated:
  configured + allowed + verified) > cheap-hosted > main-model; fallback
  recorded, total outage -> ESCALATE.
- `courier/approvals.py` — irreversible classes (send/publish/spend/delete/
  production-write/external-write) -> APPROVAL_REQUIRED, default deny.
- `courier/catalog.py` — routing projection (single JSON, atomic renames);
  CAS on integer revision; one writer seat per session with TTL, expiry
  takeover + epoch bump, explicit --steal recorded.
- `courier/ledger.py` — SQLite + JSONL mirror; idempotency key
  sha256(canonical_hash + "|" + catalog target); confirmed delivery -> duplicate
  acknowledgement. Persisted attempts distinguish rejection from unknown
  outcomes; per-key POSIX locks serialize side effects. See [delivery recovery](delivery-recovery.md).
- `courier/compactor.py` — pointer-based HandoffRecord within 2000 chars,
  newest-first, truncated flag.
- `courier/dispatcher.py` — approval gate first, then depth/paused/seat,
  ledger idempotency, backend delivery; failures hold/escalate, never force.
- `courier/adapters/base.py` — SessionBackend protocol (core codes to this).
- `courier/adapters/local.py` — in-process fake backend + fault injection.
- `courier/adapters/hermes.py` — thin CLI wrapper (`hermes chat --oneshot
  --resume <id>`) + `pre_gateway_dispatch` hook shape; honest UNKNOWNs in
  its docstring.
- `courier/cli.py` — verbs: route | inspect | topics | show | correct |
  merge | split | pause | resume | forget | approvals | doctor | status.
  Exits 0 ok | 2 usage | 3 conflict | 4 lease | 5 integrity.

## Data contracts

Message, SessionRef, RouteDecision, DeliveryRecord, HandoffRecord,
ModelConfig — field names frozen per spec; see module docstrings and
`tests/` fixtures.

## Key invariants

- Redaction runs before any router-model call.
- Router output is exactly one of the 5 kinds; provider+model always set.
- Dispatcher checks the approval gate before anything else.
- Idempotency keys are UNIQUE; duplicates ack without redelivery.
- Catalog writes need live seat + expected_rev; stale -> conflict, retry
  once, then escalate.
- Handoff payloads are pointers within budget, never transcripts/secrets.
- Telemetry/ledger store hashes/decisions/counts, never message text.
