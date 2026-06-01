create table if not exists schema_migrations (
  name text primary key,
  applied_at timestamptz not null default now()
);

create table if not exists raw_records (
  id            bigserial primary key,
  source        text not null,
  kind          text not null check (kind in ('unstructured','structured')),
  content_type  text not null,
  idempotency_key text not null,
  payload_uri   text not null,
  fetched_at    timestamptz not null,
  ingested_at   timestamptz not null default now(),
  provenance    jsonb not null default '{}'::jsonb,
  status        text not null default 'received'
                check (status in ('received','processed','unroutable','failed')),
  unique (source, idempotency_key)
);

create table if not exists work_queue (
  id           bigserial primary key,
  raw_record_id bigint not null references raw_records(id),
  state        text not null default 'pending'
               check (state in ('pending','leased','done','failed')),
  attempts     int not null default 0,
  lease_until  timestamptz not null default now(),
  last_error   text,
  enqueued_at  timestamptz not null default now()
);
create index if not exists work_queue_pending_idx
  on work_queue (state, lease_until) where state = 'pending';

create table if not exists entities (
  id             bigserial primary key,
  canonical_name text not null,
  entity_type    text not null,
  aliases        jsonb not null default '[]'::jsonb,
  is_tracked_symbol boolean not null default false,
  unique (canonical_name, entity_type)
);

create table if not exists tags (
  id            bigserial primary key,
  raw_record_id bigint not null references raw_records(id),
  source        text not null,
  observed_at   timestamptz not null,
  tag_type      text not null,
  raw_value     text not null,
  canonical_entity_id bigint references entities(id),
  score         jsonb not null default '{}'::jsonb
);
create index if not exists tags_type_value_idx on tags (tag_type, raw_value);
create index if not exists tags_observed_idx on tags (observed_at);

create table if not exists tracked_symbols (
  id          bigserial primary key,
  key         text not null unique,
  kind        text not null,
  entity_id   bigint references entities(id),
  unit        text,
  description text
);

create table if not exists observations (
  tracked_symbol_id bigint not null references tracked_symbols(id),
  ts_bucket   timestamptz not null,
  value       double precision not null,
  sample_count int not null default 0,
  primary key (tracked_symbol_id, ts_bucket)
);
