# Polymarket producer

Fetches a Polymarket event's tradable-outcome price history and submits one
`numeric-series-v1` snapshot per variant via the Bellwether ingestion API.

## What it does

Given a Polymarket event URL, the producer:
1. Resolves the URL to an event slug and calls the Gamma API (`/events?slug=`)
2. Iterates over each market within the event; for each, expands the two
   outcomes (e.g. Yes / No) into *variants* via the `clobTokenIds` list
3. Fetches the CLOB price history for each variant token
4. Submits **one immutable `numeric-series-v1` snapshot per variant** via
   `client.ingest_batch`; the worker routes it through the generic normalizer
   → `gold.upsert_value` → `observations`

## Params

| Param      | Type   | Required | Default | Notes |
|------------|--------|----------|---------|-------|
| `url`      | `str`  | yes      | —       | Polymarket event URL, e.g. `https://polymarket.com/event/<slug>` |
| `backfill` | `str`  | no       | `"all"` | `"all"` → CLOB `interval=max` (full history); `"recent"` → `interval=1d` |

## Dry-run (no network, no DB)

```bash
BELLWEATHER_TEMPLATES_DIR=producers uv run bellweather run-template \
  --template polymarket \
  --dry-run \
  --params '{"url": "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by", "backfill": "recent"}'
```

## End-to-end runbook

```bash
make up && make migrate              # start Postgres + fake-GCS; apply 0002 (producer_schedules)
bellweather seed-polymarket-demo     # insert the polymarket-demo schedule (idempotent)
bellweather orchestrate --once       # claim the due schedule -> spawns the producer subprocess
                                     #   -> producer POSTs numeric-series-v1 to /ingest
bellweather worker --once            # generic normalizer -> upsert_value -> observations
bellweather ui                       # Schedules page lists the schedule
                                     # Symbols page shows the market-probability price series
```

### Worked example — `us-x-iran` market

After seeding and running end-to-end, you should see:

- `tracked_symbols` rows with `symbol_key` like
  `polymarket:us-x-iran-permanent-peace-deal-by:<token_id>`,
  `symbol_kind = "market-probability"`, `unit = "probability"`
- `observations` rows where each value is the YES/NO implied probability in `[0, 1]`
- The Symbols UI page shows the time-series for each outcome token

## Snapshot idempotency (spec §6.1)

One record per (symbol, fetch):

```
idempotency_key = f"{symbol_key}:{sha1(canonical-json(points))}"
```

where `canonical-json(points)` is the list of `{ts, value}` dicts sorted by `ts`.

- **Identical re-fetch** → same hash → dedup (no-op; bronze snapshot already exists)
- **New/gap-filled point** → different hash → new immutable bronze snapshot →
  re-normalized; gold is safe because `upsert_value` is set-semantics (last-value-wins
  per bucket, spec §13 D1)

## Subprocess isolation (decision K4)

The orchestrator spawns the producer in a subprocess with only `BELLWEATHER_API_URL`
and `BELLWEATHER_TEMPLATES_DIR` set (plus inert placeholder DB/bucket env vars).
The producer can only `POST /ingest`; it has no direct DB or GCS access.

## ⚠ Verify caveats

1. **Template/param schema** — the seed command (`seed-polymarket-demo`) passes
   `template="polymarket"` and `params={"url": ..., "backfill": "all"}`. These
   must match the `name` and `[params]` keys in `producers/polymarket/template.toml`
   (T31). If T31 used different names, update the seed constants in `cli.py` to match.

2. **Event URL** — `https://polymarket.com/event/us-x-iran-permanent-peace-deal-by`
   is a live market that may close or be renamed. If it 404s at demo time, swap any
   current event URL into the `POLYMARKET_DEMO_URL` constant in `src/bellweather/cli.py`.

3. **API shape** — Gamma and CLOB endpoint URLs and field names are verified in
   `producers/polymarket/fetch.py` (T30). Check there first if responses change.
