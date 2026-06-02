# Reference GDELT GKG producer

A standalone, **external** producer that demonstrates the Bellwether ingest
contract. It fetches a GDELT Global Knowledge Graph (GKG) 2.1 batch, normalizes
each tab-delimited row into the `payload` shape consumed by the
`gdelt-gkg-v2` extractor (T10), and submits the batch through
`BellwetherClient`. It uses nothing privileged — only the public ingest API.

## Run

```bash
# Local GKG file:
uv run python -m producers.gdelt.producer path/to/batch.gkg.csv

# Live feed (see "Live feed" below):
uv run python -m producers.gdelt.producer http://data.gdeltproject.org/gdeltv2/20240115093000.gkg.csv
```

The script prints a one-line summary of `IngestResult` statuses
(`created` / `duplicate` / `unroutable`). The target API is taken from
`BELLWEATHER_API_URL` (via `get_settings()`); construct
`BellwetherClient(base_url=...)` and pass it to `run_path(...)` to override.

## Live feed

GDELT publishes a new GKG batch every 15 minutes. The master file list:

```
http://data.gdeltproject.org/gdeltv2/masterfilelist.txt
```

Each line is `size MD5 URL`; pick a `*.gkg.csv` (or `*.gkg.csv.zip`) URL and
point the producer at it. The producer reads plain `.gkg.csv`; for zipped
batches, unzip first and pass the local file path.

## Idempotency

`idempotency_key` is the GKG record id (column 0, e.g. `20240115093000-1`),
so re-running the same batch yields `duplicate` results rather than re-ingesting.

## Column verification caveat

The producer maps GKG 2.1 columns by index. **These indices must be verified
against the current GDELT GKG 2.1 codebook** before trusting output:

http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf

The four list fields (themes/locations/persons/organizations) use the **V2
Enhanced** columns, whose internal format is `Name,offset;Name,offset`. The
producer strips the trailing `,offset` and keeps the `;`-separated values (for
`v2_locations` each value is a full `#`-delimited location record, not a bare
name). Verified canonical
2.1 layout used here:

| Index | Field                    | Payload key        |
|-------|--------------------------|--------------------|
| 0     | GKGRECORDID              | `gkg_record_id`    |
| 1     | V2.1DATE                 | `date`             |
| 8     | V2EnhancedThemes         | `v2_themes`        |
| 10    | V2EnhancedLocations      | `v2_locations`     |
| 12    | V2EnhancedPersons        | `v2_persons`       |
| 14    | V2EnhancedOrganizations  | `v2_organizations` |
| 15    | V1.5Tone                 | `v15_tone`         |

`v15_tone` is kept as the raw comma list; the first value is the overall tone.

## As an orchestrator template

`producers/gdelt/template.toml` registers this producer with the Bellwether
orchestrator (`docs/specs/2026-06-01-producer-orchestrator-design.md` §4). The
manifest declares one parameter, `url` (a GKG file URL or local path — a
master-file entry), and a default schedule interval of `15m` (GDELT publishes a
new GKG batch every 15 minutes).

The template entrypoint is `run(params, client)` (the locked
`def run(params: dict, client) -> dict | None` contract); the older
`run_path(path_or_url, client=None)` helper is what the `python -m` CLI above
uses. GDELT is an **unstructured** feed — submissions keep
`content_type="gdelt-gkg-v2"` and flow to the existing extractor (themes /
persons / orgs / locations / tone → tags); it does **not** emit
`numeric-series-v1`.

Dry-run it through the run-harness without any datastore (points the harness at
this repo's `producers/` dir and uses a local GKG file, so no network call):

```bash
BELLWEATHER_TEMPLATES_DIR=producers \
  uv run bellweather run-template --template gdelt --dry-run \
  --params '{"url": "tests/fixtures/gkg_sample.csv"}'
```

The summary line reports `submitted` (one per GKG row) and a `sample` of the
would-be submissions; nothing is committed and no HTTP is made (`DryRunClient`).

## Go-live: run it under the orchestrator

The orchestrator (T24) can run this producer on a schedule instead of you invoking
it by hand. The `gdelt` template manifest (`producers/gdelt/template.toml`, T28)
declares the entrypoint and params; a **schedule** binds it to a concrete GKG
source (a URL or local path) and a 15-minute interval. `BELLWEATHER_TEMPLATES_DIR`
defaults to `producers`, so the manifest is discovered automatically.

`seed-gdelt-demo` binds the schedule to the bundled plain-text sample
(`tests/fixtures/gkg_sample.csv`) rather than a live URL — the producer reads plain
`.gkg.csv` and does **not** unzip, while the live GDELT feed serves only
`*.gkg.csv.zip` (see "Live feed" above). That makes the walkthrough below complete
out of the box without network access or zip handling. To run it against real data,
download a `*.gkg.csv.zip` from the master file list, unzip it, and point the schedule
at the local file (or a reachable plain-csv URL) via the Schedules UI's edit/override.

Seed the demo schedule and drive one full pass locally (run from the repo root, so
the relative sample path resolves):

```bash
make up                          # Postgres 16 + fake-gcs-server
bellweather migrate              # applies 0001 + 0002 (creates producer_schedules)
bellweather seed-gdelt-demo            # inserts the 'gdelt-demo' schedule bound to the bundled sample (idempotent — safe to re-run)

# In a second terminal, start the ingest API the producer POSTs to:
bellweather api                  # http://localhost:8000  (BELLWEATHER_API_URL)

# Front of the pipe: the orchestrator finds the due 'gdelt-demo' schedule,
# spawns the gdelt template in a subprocess (BELLWEATHER_API_URL only — no DB/bucket
# creds), which reads the GKG batch and POSTs each row to /ingest (bronze + queue):
bellweather orchestrate --once

# Back of the pipe: the worker drains the queue, routes the unstructured records to
# the gdelt-gkg-v2 extractor, and writes tags (silver) + observations (gold):
bellweather worker --once
```

Then open the UI (`bellweather ui`):
- **Schedules** page lists `gdelt-demo` (template `gdelt`, 15m interval, enabled) and its run
  history; use **Run now** / **Force Run** to trigger another pass without waiting for the interval.
- **Dashboard** shows the new tags and the observations the extractor wrote.

`seed-gdelt-demo` is idempotent (it skips if a schedule named `gdelt-demo` already exists), so it is
safe to run on every deploy. In GCP the every-minute Cloud Scheduler ping drives `orchestrate`
and `worker` automatically; you only seed once.

> ⚠ The seeded schedule points at the **bundled local sample**, which exists in the repo/dev
> checkout but **not** in the deployed Cloud Run image. It demonstrates the local end-to-end pass;
> it does not pull live data. For a real feed, override the schedule's source from the Schedules UI
> with a reachable plain-`.gkg.csv` URL (or a downloaded + unzipped batch from the master file list
> above) — the producer does not unzip `*.gkg.csv.zip`.
