# Orda operator setup (v0.1, CLI-core)

Audience: technical operators. For the non-technical guide, see `docs/guide.md`.

## What v0.1 is

CLI-core only: intake → redact → router → dispatcher → catalog projection → ledger → approvals → compactor → policy → CLI verbs. Stdlib-first Python package `courier`. No ORM. No web framework in core.

Work state is never owned here. Eldunari `~/.hermes/eldunari/nexus/state/orda/` is the only work-state store (`events.jsonl` truth, `state.json` materialization, seat lease, CAS). Orda owns a routing projection plus a delivery ledger only.

Later, explicitly out of v0.1: desktop dashboard UI, auto topic-learning, embedding tier by default, compaction/fold of ledger history.

## Install

Prerequisites: Python 3 with stdlib `sqlite3`. No required third-party deps. The only optional dep is a pluggable LLM client for the router/main tiers.

```sh
git clone <repo-url>
cd protean-ordapilot
pip install -e .
courier doctor
```

`courier doctor` pings each configured tier and prints the resolved chain, plus seat and ledger health. Fix failures before routing live traffic.

No local servers are started for you. If you want a local tier (Ollama, LM Studio, llama.cpp-family), install and serve it yourself, then point config at it. Orda never auto-starts it.

## Config (`courier.config.yaml`)

All routing and model choices are explicit config. Absent key means the feature is disabled, never silently defaulted. Environment sniffing is not used.

```yaml
router:
  provider: "<router-provider>"
  model: "<router-model>"
main:
  provider: "<main-provider>"
  model: "<main-model>"
# local:                        # optional; only when YOU run the server
#   provider: custom
#   model: "<local-tag>"
# cheap:                        # optional second cheap tier
#   provider: "<cheap-provider>"
#   model: "<cheap-model>"
# free:                         # verified-free path only; see policy below
#   provider: "<free-provider>"
#   model: "<free-model>"
free_tier_allowed: false   # default OFF; verified-free path only when true AND verified
free_verified: false        # set true only after verifying the free path against live docs/source
timeout_s: 8.0
confidence_floor: 0.65
```

### Router model policy (precedence, fixed)

1. `local-configured` — provider/model you pinned in `courier.config.yaml`.
2. `verified-free` — only if `free_tier_allowed: true` AND the free path was verified at startup against live docs/source. Default OFF. Do not promise a free path you have not verified.
3. `cheap-hosted` — config-listed cheap tier.
4. `main-model` — fallback, always available.

No silent substitution. Router tier and main tier are configured independently; the router never borrows the main tier implicitly. On tier timeout/error, policy returns the next tier and marks `fallback_from` in the decision record. Total outage escalates with reason `provider-unavailable`.

Verified Hermes-side patterns for a second cheap tier (all config keys only): a `model_aliases:` direct alias with its own `base_url` + `key_env`/`api_key` (e.g. a `router-small` alias); a `delegation:` block (`model`, `provider`, `base_url`, `api_key`, `api_mode`); or an `auxiliary.*` per-task override. Local Ollama example: `model.provider: custom`, `model.default: <tag>`, `model.base_url: http://localhost:11434/v1`, `model.context_length: 64000` (Hermes requires ≥64k for agent use; start the server yourself, e.g. `OLLAMA_CONTEXT_LENGTH=64000 ollama serve`). LM Studio: provider `lmstudio`. Generic: `base_url: http://localhost:8000/v1`. llama.cpp-family warning: full toolset plus prompt can exceed 32k context; restrict accordingly.

NOT verified: any Nous Portal free-model/trial path. Portal docs describe a paid subscription only. Treat free Portal as UNKNOWN/no.

### Named constants (defaults; override via config file only)

`ROUTER_TIMEOUT_S=8.0`, `ROUTER_RETRY_ONCE=true`, `ROUTER_CONFIDENCE_FLOOR=0.65`, `AMBIGUOUS_MARGIN=0.10`, `MAX_ROUTE_DEPTH=4`, `HANDOFF_BUDGET_CHARS=2000`, `SESSION_SUMMARY_BUDGET_CHARS=500`, `TOPIC_SEARCH_K=5`, `SEAT_TTL_S=600`, `LEDGER_PATH=<profile>/orda/ledger.sqlite` + `ledger.jsonl` mirror, `PROJECTION_PATH=<profile>/orda/routing_projection.json`.

### Privacy / redaction (runs BEFORE any router-model call)

Deny-by-default classes with patterns from explicit config: `api_keys`, `private_keys`, `tokens`, `config_emails` (only when `redact_emails: true`), plus user `extra_patterns`. Action: replace with `[REDACTED:<class>]`, count per class in `redaction_report`. If `strict: true` and any deny-class match, block the router call and hold for the user. Telemetry and ledger store hashes/decisions/counts only — never message text. Handoff bundles inherit the same redaction pass.

Approval gate: irreversible classes (send/publish/spend/delete/production-write/external-write) NEVER auto-route. `APPROVAL_REQUIRED`, default deny, explicit user confirm. Approval is an explicit user verb, never inferred.

## CLI verbs (exact)

Exit codes: 0 ok | 2 usage | 3 conflict | 4 lease | 5 integrity.

```
courier route <text>
courier inspect <message-id>
courier topics
courier show <slug>
courier correct <message-id> --to <slug>
courier merge <slug-a> <slug-b> --into <slug>
courier split <slug> --at <message-id> --new <slug>
courier pause <slug>
courier resume <slug>
courier forget <slug> [--drop-ledger]
courier approvals
courier doctor
courier status
```

Notes: `correct` re-routes plus a ledger amend event. `merge` is CAS-guarded and records a merge event. `split` creates a session behind the scenes. `pause` holds new messages for the topic into the clarification park. `forget` removes the projection entry; ledger rows are anonymized unless `--drop-ledger` with explicit confirm. Router output is exactly one of `CONTINUE | NEW | DIRECT | HANDOFF | ESCALATE` with fields `kind, target, reason, confidence, required_context, provider, model`.

## Hermes integration surface (verified)

Two supported touchpoints, both verified in Hermes v0.21.3 source:

1. **Gateway hook `pre_gateway_dispatch`** (intake interception). Fired once per incoming `MessageEvent` in `gateway/run_inbound.py`, before auth/pairing. Return `{"action": "skip"}` to drop, `{"action": "rewrite", "text": ...}` to replace text, `"allow"`/None for normal dispatch. This is the Orda intake interception point. Sibling observer-only event `gateway_platform_event` (reactions/edits/thread events) does not carry intake.
2. **CLI wrapper** around `hermes chat` / one-shot resume. Continuation contract: `hermes --resume <id|title|latest>` composes with `hermes -z PROMPT` / `hermes chat -q … --oneshot`. Verified guarantees: id/title/latest resolution, raise-on-unknown (never silently forks), compression-chain redirect, `ended_at` reopen, stored runtime restored unless `--model` is explicit. Session lookup via in-process `SessionDB.find_session_by_origin` / `list_sessions_rich`; message append via `append_message`. CLI verbs `hermes sessions list|browse|rename|delete|export|prune|stats`, `hermes model`, `hermes config set`, `hermes webhook subscribe`, `hermes send`.

Plugin registration (verified): `register_cli_command` for ops surface plus the hook above, backed by a second cheap model call. Canonical store is `$HERMES_HOME/state.db` (SQLite + FTS5) plus `sessions/` dir; profiles isolate under `$HERMES_HOME/profiles/<name>/`.

Dashboard reality-check (verified): the web dashboard (`hermes dashboard`) and the desktop app both expose real plugin surfaces (dashboard manifest + `/api/plugins/<id>/`; desktop `$HERMES_HOME/desktop-plugins/<id>/plugin.js`). Neither is a message-path injection point. A dashboard tab alone cannot intercept messages. Dashboard UI is explicitly later for Orda v0.1.

Gateway middleware is NOT applicable (only tool/llm kinds; no inbound-message kind). Webhook routes with `--script` pre-filter are a limited platform-scoped alternative, not the general path.

## Honest UNKNOWNs (do not build on these)

- Per-channel → profile binding contract (`profile_channels.py` exists; contract unread).
- Nous Portal free-model/trial path (no free-tier string in portal docs; subscription-only as documented).
- Concurrent-resume locking under two writers; exact `assert_resume_safe` rejection conditions.
- Standalone public sessions REST/SDK surface (only in-process `SessionDB` + CLI verbs verified).
- `pre_gateway_dispatch` latency budget / timeout behavior on slow hooks.
- Whether `pre_gateway_dispatch` fires for CLI-local turns or only gateway platform events (fire site is the gateway inbound mixin; CLI path not traced).
- OPEN-1 Hermes adapter binding choice (CLI-wrap vs in-process plugin hook): adapter stays pluggable pending adjudication.
- OPEN-2 verified-free tier allowlist: which free provider/model paths are real.
- OPEN-3 embedding tier for topic layer B: allowed provider + budget, or keyword-only for v0.1.
- OPEN-4 clarification UX: inline question vs approvals-queue entry for parked ambiguous messages.

## Limits in v0.1 (verified)

- Topic identification is layered simple-first: explicit slug (score 1.0) > metadata + keyword over summaries (exact slug/substring, Jaccard keyword overlap, top-K=5) > escalate. No full-transcript scanning by the router; summaries ≤500 chars only.
- Embedding cosine over summaries exists only if configured. Default v0.1 path is keyword matching. No embeddings by default.
- Confidence floor 0.65; top-2 margin under 0.10 means ambiguous bucket, never a guess. Escalation ladder is fixed: small router (8s budget) → one retry into the ambiguous bucket → main-model decide → user clarification with the top-2 slugs. Every step records provider+model, never secrets.
- Loop prevention: `route_depth` per message chain, max 4; overflow forces `ESCALATE` with reason `route-depth-overflow`.
- Delivery ledger: JSONL + SQLite, idempotency key `sha256(canonical_message_hash + "|" + target_session_id)`; only confirmed delivered keys return duplicate acknowledgements. Rejected and uncertain attempts require [explicit recovery](delivery-recovery.md). SQLite is authoritative; JSONL mirrors state transitions. Records retain hashes and opaque reference hashes, never message text or adapter exception contents.
- Catalog writes use CAS on integer `revision`; stale `expected_rev` means exit 3, one retry, then escalate — never silent overwrite. One writer seat per session; busy seats park, expired seats allow takeover with epoch bump, live seats refuse unless explicit `--steal` (recorded).

STABLE
