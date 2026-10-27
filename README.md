# Transaction Ledger API (FastAPI + Postgres)

## Run locally
```bash
pip install -r requirements.txt
# Run schema.sql in Supabase SQL editor (or psql)
export DATABASE_URL="postgresql://user:pass@host:5432/postgres"
uvicorn main:app --reload      # docs at http://localhost:8000/docs
```

## Tests
Run against a throwaway database (never production):
```bash
pip install -r requirements-dev.txt
export DATABASE_URL="postgresql://user:pass@localhost:5432/ledger_test"
pytest -v
```
Covers validation, duplicates, insufficient balance, pagination/filter/sort, transfers, rollback, and concurrent debits/duplicates.

## Endpoints
| Method | Path | Notes |
|---|---|---|
| POST | /accounts | `{"name": "Esha"}` -> 201. Extra fields (e.g. balance) rejected with 422 |
| GET | /accounts/{id}/balance | `{account_id, balance}`; 404 if missing |
| POST | /accounts/{id}/transactions | `{type, amount, description, reference_id}` -> 201 |
| GET | /accounts/{id}/transactions?type=&page=&limit= | latest first; `pagination: {page, limit, total_records, total_pages}` |
| POST | /transfers | `{from_account_id, to_account_id, amount, reference_id, description}` (bonus) |

No PUT/PATCH/DELETE exist for transactions; a DB trigger also blocks UPDATE/DELETE.

## Errors
Shape: `{"error": {"code", "message", "details?"}}`

| Status | Code | When |
|---|---|---|
| 404 | ACCOUNT_NOT_FOUND | unknown account |
| 409 | DUPLICATE_TRANSACTION | same reference_id on same account (existing txn returned in details; balance untouched) |
| 422 | VALIDATION_ERROR | bad type/amount/name/UUID/pagination/body |
| 422 | INSUFFICIENT_BALANCE | debit > balance |
| 422 | INVALID_TRANSFER | same source and destination |

## Design
- **Atomicity**: insert txn + update balance in one DB transaction.
- **Concurrency**: `SELECT ... FOR UPDATE` on the account row serialises writes, so no lost updates or double-spends. Transfers lock both rows in id order to avoid deadlocks.
- **Idempotency**: `UNIQUE (account_id, reference_id)` is the final guard; an app-level check gives a clean 409.
- **Integrity constraints**: `balance >= 0`, `amount > 0`, `type IN (credit, debit)`, FK on account_id.
- **Money**: `NUMERIC(14,2)` / `Decimal`, never floats; amounts max 2 decimal places, returned as 2dp.

## Assumptions
- Single currency; amounts have max 2 decimal places.
- Duplicate reference_id returns 409 (not 200) with the original transaction.
- reference_id is trimmed and case-sensitive.
- Transfer reference_id is used on both legs (debit and credit), linked by `transfer_id`; duplicates are detected on the source account.
- No auth; page size capped at 100; page beyond the last returns an empty list.
- Invalid UUID in path returns 422 validation error.
