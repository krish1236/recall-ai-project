# Recall Mission Control

A customer-call intelligence inbox built on top of [Recall.ai](https://recall.ai). Real bot joins a real meeting, real-time signals surface while the call is live, and a deep operator view shows every webhook, every retry, every millisecond of latency — with a deterministic Replay button that rebuilds derived state from the event log.

Two audiences in one product:

- **Sales / CS rep** sees the Inbox, the Live Call view, and the Intelligence card. They copy the follow-up email, push to the mock CRM, and move on.
- **Infrastructure engineer** opens **Mission Control** and sees the event timeline, webhook deliveries, state transitions, latency waterfall, and DLQ. Click *Replay* and the whole derived state wipes and rebuilds from the source-of-truth event log.

---

## What's here

```
recall-ai/
  apps/
    api/          FastAPI backend, Alembic migrations, worker, intelligence layer
    web/          Next.js 16 + Tailwind 4 frontend (App Router)
  infra/
    docker-compose.yml   Postgres 16 + Redis 7 on alt ports (55432, 56379)
  scripts/
    send_fake_webhook.py Dev helper: fires signed webhook at the local ingest
```

**Stack:** FastAPI · SQLAlchemy 2 · Alembic · Postgres 16 (tsvector + JSONB) · Redis 7 (Streams + Pub/Sub) · Next.js 16 · Tailwind 4 · Claude Haiku (classification) + Sonnet (synthesis) · Cloudflared (dev tunnel).

**Tests:** ~94 pytest cases across unit + integration + HTTP + WebSocket paths.

---

## Quickstart

```bash
# 1. local infra — Postgres + Redis on alt ports so they don't collide with
#    anything you already run on 5432/6379
make infra.up

# 2. python deps + migrations
cd apps/api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head

# 3. frontend deps
cd ../web && npm install

# 4. copy .env and paste your keys (RECALL_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY)
cp .env.example .env   # then edit

# 5. expose the local api so Recall can deliver webhooks
cloudflared tunnel --url http://localhost:8000
#    paste the printed https URL into WEBHOOK_PUBLIC_URL in .env (with /webhook/recall appended)

# 6. run the three processes
make api       # terminal 1
make worker    # terminal 2
make web       # terminal 3 → http://localhost:3000
```

---

## Architecture at a glance

```
┌──────────────────────────────────────────────────────────────────────┐
│                        External: Recall.ai                           │
│   POST /api/v1/bot     ◄───── our backend                            │
│   webhooks (Svix-signed) ──►  https://<tunnel>/webhook/recall        │
└──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         Backend (FastAPI)                            │
│   /webhook/recall    → verify svix sig → dedupe → persist → XADD     │
│   /meetings          → POST, GET, list, finalize, crm-push           │
│   /meetings/{id}/ops → Mission Control data                          │
│   /live/{id}         → WebSocket (Redis Pub/Sub)                     │
│   /admin/dlq, /admin/replay                                          │
│                                                                      │
│   Worker: XREADGROUP across per-bot streams, event-time sort,        │
│           handlers (lifecycle, projector, batcher), DLQ on failure   │
│                                                                      │
│   Intelligence:                                                      │
│     Tier 0 — regex prefilter (free, sub-ms)                          │
│     Tier 2 — Claude Haiku (classification, per-batch)                │
│     Tier 3 — Claude Sonnet (post-call synthesis)                     │
│              all wrapped in a prompt-hash cache + circuit breaker    │
└──────────────────────────────────────────────────────────────────────┘
```

Source of truth: `meeting_events` (immutable, append-only). Everything downstream — `meetings.status`, `transcript_utterances`, `insights`, `insight_evidence`, `action_items`, `summaries` — is a projection that Replay can rebuild deterministically.

---

## The ten hard problems (and how they're solved)

| # | Problem | Where it lives |
|---|---|---|
| HP-1 | Exactly-once ingestion under 60-attempt retry storms | `apps/api/webhook.py` (svix-id + content hash, `UNIQUE` index) |
| HP-2 | Per-meeting ordering + cross-meeting parallelism | `apps/api/streams.py` (Redis Streams per `bot_id`) + `worker.py` |
| HP-3 | Deterministic replay of a meeting | `apps/api/replay.py` (wipes derived rows, re-dispatches events) |
| HP-4 | Out-of-order event reconciliation | handlers sort batches by `event_timestamp`; state machine is monotone |
| HP-5 | Backpressure-aware LLM batching | `apps/api/intelligence/batcher.py` (size / time / fast-path triggers) |
| HP-6 | Multi-turn signal detection | classifier prompt carries last 6 prior utterances + evidence linking |
| HP-7 | Dead-letter visibility + circuit breaker | `apps/api/admin.py`, `intelligence/breaker.py` |
| HP-8 | Signature-fail-closed, log-open | webhook stores every delivery (valid or invalid) to `webhook_deliveries` |
| HP-9 | Per-utterance latency waterfall | `utterance_spans` table + `ops.py` P50/P95/P99 |
| HP-10 | Tier 0 prefilter (bonus — saves LLM spend) | `apps/api/intelligence/prefilter.py` |

---

## Challenges a production meeting-intelligence pipeline actually faces

The prototype handles a lot, but "production-ready at Recall scale" is a different bar. Here's the honest view — what we built, what we didn't, and why.

### 1. Network / delivery layer

**Challenge:** webhooks arrive out of order, get retried 60× under Recall's policy, tunnels flake, load balancers drop mid-POST.

**What we built:**
- Svix signature verification with `webhook-id` / `svix-id` idempotency key
- Content-hash dedupe fallback (SHA-256 over `bot_id | event_type | event_timestamp | canonical_json`)
- `UNIQUE` index on `meeting_events.dedupe_key` → ON CONFLICT DO NOTHING
- Every HTTP hit (valid or invalid sig) written to `webhook_deliveries` for audit

**What production adds:**
- Outbox pattern on the sender side (not under our control; Recall does this)
- Multi-region webhook endpoints with geo-aware DNS so delivery doesn't cross regions
- Alerting on `webhook_deliveries` anomalies (sudden spike of bad_sig could be misconfigured customer)

### 2. Meeting-platform quirks

**Challenge:** Zoom, Meet, Teams, Webex, GoTo each have their own bot SDK, admit-flow, caption availability, waiting-room semantics. This is an integration hell that *is* Recall's moat.

**What we built:**
- Our `RecallClient` is platform-agnostic — we pass a URL, Recall picks the right bot
- Graceful handling of the `in_waiting_room → joining_call → in_call_recording` state progression

**What production adds (and what Recall themselves handle):**
- Platform-specific auth review / marketplace listings
- Audio/video codec negotiation per platform
- Separate-track recording quality (Recall's pitch)
- Compliance screening (some Teams tenants block bots at org level)

### 3. Transcription quality

**Challenge:** captions lie. Homophones, speaker drift, crosstalk, non-English stretches, missing words. Whatever your classifier sees is what was transcribed, not what was said.

**What we built:**
- Graceful speaker-label fallback chain (`participant.name → participant.id → data.speaker → words[0].speaker`)
- Multi-path payload extraction for the real Recall shape (`payload.data.data.words`) vs a flatter shape for fixtures
- `source_event_id` on every utterance so we can trace back to the raw payload

**What production adds:**
- Word-level confidence scores threshold (drop below 0.6 before showing to classifier)
- Language-detection + degrade-gracefully for non-English stretches
- Speaker consolidation heuristics (voice fingerprint / positional) to undo "speaker 3, 4, 3, 4" jitter
- Partial-utterance replacement in UI (current: partials are UI-only and dropped on finalize)

### 4. Event ordering and clock skew

**Challenge:** `transcript.data` at t=15s arrives after t=20s; provider clock drifts relative to yours; late finalized-replaces-partial arrives seconds after the fact.

**What we built:**
- Redis Stream per `bot_id` guarantees per-meeting FIFO at the transport layer
- Worker reads a batch, sorts by `event_timestamp` (HP-4) before dispatching
- State-machine guard: new status must have `event_timestamp >= meeting.state_changed_at`, else drop

**What production adds:**
- Watermark-based windowing (à la Flink) for exactly-once stateful aggregations
- Hybrid-logical-clock timestamps when you care about cross-region ordering
- A "quiet period" after `bot.done` so stragglers don't get silently dropped

### 5. Load, backpressure, and LLM collapse

**This is the one that kills companies.** An Anthropic bad minute turns into 100 stuck meetings without protection.

**What we built:**
- **Per-meeting batching** with three flush triggers (size ≥ 5, time ≥ 2.5s, fast-path regex)
- **Tier 0 regex prefilter** (`intelligence/prefilter.py`) — skips LLM entirely when a batch has no lexical signal hint, cutting ~70% of calls on realistic transcripts
- **Prompt-hash cache** — replays and repeat prompts serve from DB, zero LLM cost
- **Circuit breaker** (`intelligence/breaker.py`) — rolling 30-call window, opens at 30% error rate, half-open probe after 30s. Shared across classifier + synthesizer so one breaker guards all LLM traffic
- **DLQ with circuit_open status** — when the breaker is open, batches land in the dead-letter queue rather than queueing up in memory. Operator can replay from the UI once the breaker closes
- **Bounded concurrent flushes** (semaphore) so a runaway meeting can't starve the process

**What production adds (and the economic reasons):**
- **Tier 1 local classifier** (DistilBERT or TF-IDF+logreg) between Tier 0 and the LLM. At volume, most false-positives from Tier 0 regex get rejected here before spending tokens. Not built: adds ONNX runtime dependency + CPU/GPU scheduling surface, overkill for single-process demo.
- **Per-customer rate shaping** — one noisy customer can't saturate shared LLM quota
- **Reserved throughput contracts** with Anthropic/OpenAI at 50-70% off list
- **Incremental synthesis** — feed live insights INTO the post-call synthesizer instead of re-processing the full transcript. The current architecture double-pays (once live, once in synthesis). Roughly 3-5× cost reduction on synthesis.
- **Distributed circuit-breaker state** via Redis, so 3 replicas share breaker state rather than each observing 10 failures independently

The business-model math: a typical SaaS layer on top of Recall prices around **$2K/seat/year**. A rep does ~30 meetings/month × 12 months = 360 meetings/year. At naive Haiku costs (~$0.80/meeting without the Tier 0 filter), that's $288/seat/year in LLM spend alone — **14% of revenue, half your margin on a SaaS-typical cost structure.** The Tier 0 filter and cache together drop that to ~$0.15/meeting, which is the difference between a healthy business and an LLM-subsidized one.

### 6. Bot lifecycle edge cases

**Challenge:** bot silently disconnects mid-call, meeting runs over, host kicks the bot, recording without consent.

**What we built:**
- Explicit state machine (`requested → joining → in_call → processing → done | failed`)
- Out-of-order guard so a late `in_call` can't regress a `done` meeting
- `meetings.ended_at` set on both `done` and `failed` for consistent UI

**What production adds:**
- Independent liveness polling (`GET /bot/:id` every 30s) so we notice disconnects without relying on a dead event stream
- Consent banner + audio cue (legal requirement in two-party-consent states and GDPR jurisdictions)
- Bot-status webhook configured separately from realtime-endpoints (we currently don't consume bot lifecycle webhooks; finalize is manual)

### 7. State-machine and temporal consistency

**Challenge:** "when does a meeting start?" / "when does it end?" have no universally right answer. A straggler `transcript.data` at 5:10:00 carrying `event_timestamp=5:00:30` — re-open? Drop? Silent append?

**What we built:**
- Event-sourcing + projections: `meeting_events` is the source of truth, everything derived
- Replay engine (`replay.py`) wipes derived rows and rebuilds from the event log — deterministic thanks to LLM cache
- Monotone-timestamp guard in the lifecycle handler

**What production adds:**
- Configurable grace window after `done` (e.g., 60s) during which late events still append
- Audit log of every state transition with who/what/when for regulated industries
- Idempotent late-event handling that can replace a stale partial with a finalized one

### 8. Observability

**Challenge:** "my meeting didn't capture the discount discussion" — was it transcription, classification, or LLM? Without per-utterance tracing, you're guessing.

**What we built:**
- `utterance_spans` table: `persisted_at`, `enqueued_at`, `classified_at`, `pushed_at` for each utterance
- `ops.py` computes P50/P95/P99 end-to-end latency per meeting
- Mission Control page surfaces the full event timeline + webhook deliveries + DLQ
- Structured logs with meeting_id / bot_id / event_type in every line

**What production adds:**
- Prometheus `/metrics` endpoint (planned; not yet wired)
- Distributed tracing (OpenTelemetry spans across webhook → stream → worker → LLM → WS)
- Cost per meeting tracked in real time (we store `token_in`/`token_out`/`cost_usd` in `llm_cache` but don't surface it yet)
- Insight-type distribution drift monitoring (hallucination canary)

### 9. Trust / UX

**Challenge:** LLM hallucinates. User clicks "→ evidence" and gets gibberish. Confidence miscalibration creates false trust.

**What we built:**
- **Evidence linking is mandatory** — signals without resolvable `source_aliases` are dropped at persist time (`classifier.py`)
- Structured-output tool use with enum-constrained `type` so the LLM can't invent signal categories
- Confidence displayed alongside title so users know what they're trusting

**What production adds:**
- Fast-path pill in UI the moment regex matches, LLM detail fills in 500ms later (current: batching introduces up to 2.5s delay)
- Human-in-the-loop sampling: 1% of classifications flagged for manual review
- Deduplication / merging of repeated signals within a meeting (current: a customer mentioning Gong twice creates two rows)

### 10. Scale issues specific to Recall's domain

**Challenge:** multi-region data residency, noisy neighbors, platform deprecations, cost attribution at 1000s of customers.

**What we built:** largely out of scope for a demo.

**What production adds:**
- Region-pinned Postgres + LLM endpoints (Recall offers EU + US + APAC regions)
- Per-customer worker pool isolation
- API versioning with deprecation runway
- Per-meeting cost attribution exact enough to bill

---

## Trade-offs explicitly made

Some things are deliberately *not* built, for good reasons:

- **No real auth** — single-user demo. Production needs workspace isolation, SSO, role-based access.
- **No distributed worker** — one process handles everything. Production fans out by bot_id-hash across replicas.
- **Tier 1 local classifier skipped** — the decision path is documented (§5 above). The next cost-unit-economics tier isn't worth the ONNX/model-weights complexity at demo scale.
- **No insight deduplication** — customer repeats "Gong is cheap" three times → three rows. A merge pass would fix it.
- **Synthesizer re-processes the full transcript** — instead of reusing live insights. This is the biggest remaining cost leak; documented in §5.
- **CRM push is mocked** — logs to `meeting_events` as `internal.crm_pushed`. Real integration (Salesforce / HubSpot) is out of scope.

---

## Testing

```bash
make test
```

~94 pytest cases covering:

- Webhook: signature verification, content-hash dedupe under replay (50× same event → 1 row, 50 deliveries), P99 < 50ms over 1000 synthetic events
- Stream: shuffled events processed in event-time order, multi-meeting fairness
- Handlers: lifecycle state machine (normal progression, stale rejected, terminal states, monotone guard, unknown code), transcript projector (payload shape variants, speaker fallback)
- Intelligence: classifier (cache hit on replay, evidence linking, alias resolution, multi-turn context), batcher (size/time/fast-path triggers, DLQ on failure, backpressure bounds LLM calls), synthesizer (idempotent replace, skip-empty-transcript, cache), circuit breaker (all four transitions), prefilter (positive/negative patterns)
- Routes: meetings CRUD, finalize + synthesis, mock CRM push, DLQ admin, ops endpoint
- Live: WebSocket receives pub/sub frames scoped to the correct meeting

All tests are isolated — `conftest.py` truncates Postgres, flushes Redis DB 1 (tests use a separate DB from the dev worker on DB 0), and resets the circuit breaker before each test.

---

## Demo script

1. `make infra.up && make api && make worker && make web` (plus cloudflared)
2. Open http://localhost:3000 → Inbox
3. "New meeting" → paste a Google Meet / Zoom / Teams URL → "Send bot"
4. Bot joins; Live Call view fills in transcripts and signals as people speak
5. Click "End & synthesize" → status flips `in_call → processing → done`
6. Intelligence card populates with exec summary, action items, follow-up email, CRM note
7. Open Mission Control → event timeline, latency waterfall, DLQ visible
8. Click "Replay" → derived state wipes and rebuilds identically (LLM cache serves the prompts)

---

## Deployment notes (not wired)

Target: **Vercel for web, Railway for API + worker + Postgres + Redis.**

- `apps/web` → Vercel, env `NEXT_PUBLIC_API_URL`
- `apps/api` → Railway service running `uvicorn main:app`
- Worker → second Railway service running `python worker.py`, same image different CMD
- Postgres + Redis → Railway managed add-ons
- `RECALL_WEBHOOK_PUBLIC_URL` set to the production api domain

---

## Status

This is a working prototype demonstrating architecture, not a production service. The phases that shipped:

- **Phase 0**: monorepo scaffold
- **Phase 1**: Postgres + Redis infra, Alembic migrations, 12 tables
- **Phase 2**: signed + idempotent webhook receiver (HP-1, HP-8)
- **Phase 3**: Redis Streams per bot_id, worker with event-time ordering (HP-2, HP-4)
- **Phase 4**: Recall client, POST /meetings, real state machine + transcript projector
- **Phase 5**: Tier 2 classifier (Haiku) with batching, caching, multi-turn context, DLQ (HP-5, HP-6)
- **Phase 6**: Next.js frontend — Inbox, New Meeting, Live view, Intelligence card, WebSocket
- **Phase 7**: Tier 3 synthesizer (Sonnet) + finalize route + mock CRM push
- **Phase 8**: circuit breaker (HP-7), utterance latency spans (HP-9), DLQ admin
- **Phase 9**: Mission Control — event timeline, latency waterfall, deliveries, DLQ, Replay button (HP-3)
- **Phase 9b**: Tier 0 regex prefilter — the single highest-leverage cost optimization

---

## Repo

[github.com/krish1236/recall-ai-project](https://github.com/krish1236/recall-ai-project)
