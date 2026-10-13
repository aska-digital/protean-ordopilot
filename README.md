# Orda

## Names

- **Orda** — the product name users see. One plain input for getting work done with AI.

## What it is

One input for getting work done. You type what you want in plain words and press send. Orda works out what kind of request it is and handles it, then gives you the result. The backend shipped in this repository is a stand-in. It records the request and returns a fixed shape to show the interface rather than to produce model output. A live backend does that work in production. You do not manage anything. Sends, posts, spends, deletes, and shared-work writes wait for an explicit yes.

## Promise

Type what you want in plain words. Orda reads the request, does the work, and gives you the result. In this repository the result comes from the stand-in. It is a fixed shape that shows where a live model result would appear, not the model output itself. It asks when a request could mean two things, and it never sends, spends, or erases without your yes.

## Quick start

```sh
git clone <repo-url>
cd orda
pip install -e .
```

Run the checks (12 test classes: continuation, new-topic, ambiguous, escalation, compression, idempotency, busy/expired seats, CAS conflict, provider fallback, redaction, irreversible boundary, CLI path):

```sh
pytest
```

Try the CLI demo against the in-process fake backend:

```sh
courier doctor
courier topics
courier show <slug>
```

## Architecture

Spec: `docs/architecture.md` (module map, data contracts, escalation ladder, key invariants). Key points: stdlib-first `courier` package; canonical work state lives outside this package; Orda holds the routing projection and the delivery ledger; router output is exactly CONTINUE | NEW | DIRECT | HANDOFF | ESCALATE; redaction before any router call; approval gate default-deny on irreversible classes.

## Status (honest)

Works in v0.1: intake → redact → router → dispatcher → catalog projection → ledger → approvals → compactor → policy → CLI verbs (`topics`, `show`, `correct`, `merge`, `split`, `pause`/`resume`, `forget`, `approvals`, `doctor`). Keyword topic matching over short summaries. CAS-guarded writes. Delivery acknowledgement for confirmed results and [explicit recovery](docs/delivery-recovery.md) for rejected or uncertain attempts. Fixed escalation ladder.

Later, not in v0.1: dashboard UI, auto topic-learning, embedding tier by default, ledger history compaction. UNKNOWN (unverified, do not rely on): per-channel profile binding, free-tier Portal path, concurrent-resume locking, sessions REST/SDK surface, hook latency budget, CLI-local hook firing, adapter binding choice, free-tier allowlist, embedding provider, clarification UX shape.

## License

MIT License — Copyright (c) 2026 Ahraz Arifuddin. See `LICENSE`.

STABLE
