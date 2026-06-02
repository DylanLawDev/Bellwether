create table if not exists producer_schedules (
  id bigserial primary key,
  name text not null,
  template text not null,
  params jsonb not null default '{}'::jsonb,
  interval_seconds int not null check (interval_seconds > 0),
  enabled boolean not null default true,
  force_run boolean not null default false,
  last_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table if not exists producer_runs (
  id bigserial primary key,
  schedule_id bigint references producer_schedules(id),
  template text not null,
  params jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running' check (status in ('running','ok','error')),
  submitted int,
  error text
);
create index if not exists producer_runs_schedule_idx on producer_runs (schedule_id, started_at desc);
