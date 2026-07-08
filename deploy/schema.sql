create extension if not exists "pgcrypto";

create table if not exists groups (
  id          uuid primary key default gen_random_uuid(),
  name        text not null default 'New Group',
  position    integer not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  deleted_at  timestamptz
);

create table if not exists sheets (
  id          uuid primary key default gen_random_uuid(),
  group_id    uuid not null references groups(id) on delete cascade,
  body        text not null default '',
  position    integer not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  deleted_at  timestamptz
);
create index if not exists sheets_group_idx on sheets(group_id) where deleted_at is null;

-- append-only version history; every meaningful save writes (or coalesces into) a row.
create table if not exists sheet_versions (
  id          bigserial primary key,
  sheet_id    uuid not null references sheets(id) on delete cascade,
  version     integer not null,
  body        text not null,
  word_count  integer not null default 0,
  char_count  integer not null default 0,
  device      text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (sheet_id, version)
);
create index if not exists sheet_versions_sheet_idx on sheet_versions(sheet_id, version desc);
