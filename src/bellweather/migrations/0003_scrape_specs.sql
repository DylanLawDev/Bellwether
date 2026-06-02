create table if not exists scrape_specs (
  id            bigserial primary key,
  name          text not null unique,        -- referenced by a record's provenance.scrape_spec
  description   text,
  sites         jsonb not null default '[]'::jsonb,   -- list of URLs
  output_schema jsonb not null,              -- JSON Schema → LLM tool input_schema
  binding       jsonb not null,              -- see binding contract below
  fetch_adapter text not null default 'httpx',
  llm_model     text,                        -- per-spec model override; null → settings default
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
