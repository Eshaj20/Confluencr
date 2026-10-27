# Transaction Ledger API (FastAPI + Postgres)

A backend API for creating accounts, recording immutable credit and debit transactions, retrieving balances and history, and transferring money atomically between accounts.

## Live deployment

- API documentation: https://tr-4f86040cf995477798d60356f9e04a82.ecs.ap-southeast-2.on.aws/docs
- Health check: https://tr-4f86040cf995477798d60356f9e04a82.ecs.ap-southeast-2.on.aws/health

The deployed service runs on AWS ECS Express Mode. It uses a container image in Amazon ECR, retrieves `DATABASE_URL` from AWS Secrets Manager, and connects to a Supabase PostgreSQL database.

## Run locally
Create a virtual environment, install the dependencies, run `schema.sql` in the Supabase SQL Editor (or with `psql`), and set `DATABASE_URL` to a Supabase connection string. Do not commit the connection string or database password.

### PowerShell (Windows)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:DATABASE_URL = "postgresql://..."
python -m uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs.

### macOS / Linux
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://user:pass@host:5432/postgres"
python -m uvicorn main:app --reload
```

## Tests
Run against a separate test database, never the deployed demo database. Run `schema.sql` in that database first.

### PowerShell (Windows)
```powershell
python -m pip install -r requirements-dev.txt
$env:DATABASE_URL = "postgresql://..."
python -m pytest -v
```

### macOS / Linux
```bash
pip install -r requirements-dev.txt
export DATABASE_URL="postgresql://user:pass@localhost:5432/ledger_test"
python -m pytest -v
```

Verified result: **33 passed**. The suite covers validation, duplicate submissions, insufficient balance, pagination, filtering, sorting, transfers, rollback behavior, and concurrent debits and duplicate requests.

## Deployment

The repository includes a `Dockerfile` for the FastAPI service.

1. Build the image with Docker and push it to Amazon ECR.
2. Deploy the image through AWS ECS Express Mode, configured with container port `8000` and health path `/health`.
3. Store the Supabase connection string in AWS Secrets Manager as `DATABASE_URL`.
4. Grant the ECS task execution role permission to read that secret.

The local Docker engine is only used to build and push the image. The deployed service runs independently in AWS.

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
