create extension if not exists pgcrypto;

create table if not exists accounts (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(btrim(name)) > 0),
  balance numeric(14,2) not null default 0 check (balance >= 0),
  created_at timestamptz not null default now()
);

create table if not exists transactions (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id),
  type text not null check (type in ('credit','debit')),
  amount numeric(14,2) not null check (amount > 0),
  description text not null default '',
  reference_id text not null check (length(btrim(reference_id)) > 0),
  transfer_id uuid,
  created_at timestamptz not null default now(),
  unique (account_id, reference_id)
);
create index if not exists idx_txn_account_created on transactions (account_id, created_at desc);

-- Immutability enforced at the DB level (API also exposes no update/delete)
create or replace function forbid_txn_change() returns trigger as $$
begin raise exception 'transactions are immutable'; end; $$ language plpgsql;
drop trigger if exists trg_txn_immutable on transactions;
create trigger trg_txn_immutable before update or delete on transactions
  for each row execute function forbid_txn_change();
