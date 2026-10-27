import math
import os
from decimal import Decimal
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DATABASE_URL = os.environ["DATABASE_URL"]
app = FastAPI(title="Transaction Ledger API")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Amount = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


@app.get("/health")
def health_check():
    """Lightweight service health check for the deployment platform."""
    return {"status": "ok"}


# ---------- helpers ----------
class ApiError(Exception):
    def __init__(self, status, code, message, extra=None):
        self.status, self.code, self.message, self.extra = status, code, message, extra


def out(data, status=200):
    body = jsonable_encoder(data, custom_encoder={Decimal: lambda d: format(d, ".2f")})
    return JSONResponse(body, status_code=status)


def err_body(code, message, details=None):
    e = {"code": code, "message": message}
    if details is not None:
        e["details"] = details
    return {"error": e}


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, e: ApiError):
    return out(err_body(e.code, e.message, e.extra), e.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, e: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in x["loc"]), "message": x["msg"]} for x in e.errors()]
    return out(err_body("VALIDATION_ERROR", "Invalid request data", details), 422)


def db():
    # prepare_threshold=None keeps it compatible with Supabase's transaction pooler
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)


# ---------- schemas ----------
class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # rejects client-supplied balance/id/created_at
    name: Name


class TxnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["credit", "debit"]
    amount: Amount
    description: str = Field(default="", max_length=500)
    reference_id: Ref


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account_id: UUID
    to_account_id: UUID
    amount: Amount
    description: str = Field(default="", max_length=500)
    reference_id: Ref


# ---------- core logic (caller must hold the account row lock) ----------
def apply_txn(cur, acct, type_, amount, desc, ref, transfer_id=None):
    if type_ == "debit" and acct["balance"] < amount:
        raise ApiError(422, "INSUFFICIENT_BALANCE", "Insufficient balance",
                       {"balance": format(acct["balance"], ".2f"), "requested": format(amount, ".2f")})
    cur.execute(
        "insert into transactions (account_id,type,amount,description,reference_id,transfer_id) "
        "values (%s,%s,%s,%s,%s,%s) returning *",
        (acct["id"], type_, amount, desc, ref, transfer_id))
    txn = cur.fetchone()
    delta = amount if type_ == "credit" else -amount
    cur.execute("update accounts set balance = balance + %s where id=%s returning balance", (delta, acct["id"]))
    acct["balance"] = cur.fetchone()["balance"]
    return txn


def existing_txn(cur, account_id, ref):
    cur.execute("select * from transactions where account_id=%s and reference_id=%s", (account_id, ref))
    return cur.fetchone()


def duplicate(txn):
    return ApiError(409, "DUPLICATE_TRANSACTION",
                    "Transaction with this reference_id already exists for this account",
                    {"existing_transaction": jsonable_encoder(txn, custom_encoder={Decimal: lambda d: format(d, ".2f")})})


# ---------- endpoints ----------
@app.post("/accounts", status_code=201)
def create_account(body: AccountIn):
    with db() as conn, conn.cursor() as cur:
        cur.execute("insert into accounts (name) values (%s) returning *", (body.name,))
        return out(cur.fetchone(), 201)


@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: UUID):
    with db() as conn, conn.cursor() as cur:
        cur.execute("select id, balance from accounts where id=%s", (account_id,))
        a = cur.fetchone()
        if not a:
            raise ApiError(404, "ACCOUNT_NOT_FOUND", "Account not found")
        return out({"account_id": a["id"], "balance": a["balance"]})


@app.post("/accounts/{account_id}/transactions", status_code=201)
def add_transaction(account_id: UUID, body: TxnIn):
    try:
        with db() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("select id, balance from accounts where id=%s for update", (account_id,))
            acct = cur.fetchone()
            if not acct:
                raise ApiError(404, "ACCOUNT_NOT_FOUND", "Account not found")
            dup = existing_txn(cur, account_id, body.reference_id)
            if dup:
                raise duplicate(dup)
            txn = apply_txn(cur, acct, body.type, body.amount, body.description, body.reference_id)
            return out({"transaction": txn, "balance": acct["balance"]}, 201)
    except psycopg.errors.UniqueViolation:  # backstop for the (account_id, reference_id) constraint
        raise ApiError(409, "DUPLICATE_TRANSACTION", "Transaction with this reference_id already exists for this account")


@app.get("/accounts/{account_id}/transactions")
def list_transactions(account_id: UUID,
                      type: Optional[Literal["credit", "debit"]] = None,
                      page: int = Query(1, ge=1),
                      limit: int = Query(20, ge=1, le=100)):
    with db() as conn, conn.cursor() as cur:
        cur.execute("select 1 from accounts where id=%s", (account_id,))
        if not cur.fetchone():
            raise ApiError(404, "ACCOUNT_NOT_FOUND", "Account not found")
        cur.execute("select count(*) as c from transactions where account_id=%s and (%s::text is null or type=%s)",
                    (account_id, type, type))
        total = cur.fetchone()["c"]
        cur.execute(
            "select * from transactions where account_id=%s and (%s::text is null or type=%s) "
            "order by created_at desc, id desc limit %s offset %s",
            (account_id, type, type, limit, (page - 1) * limit))
        return out({"data": cur.fetchall(),
                    "pagination": {"page": page, "limit": limit, "total_records": total,
                                   "total_pages": math.ceil(total / limit)}})


@app.post("/transfers", status_code=201)
def transfer(body: TransferIn):
    if body.from_account_id == body.to_account_id:
        raise ApiError(422, "INVALID_TRANSFER", "Source and destination accounts must differ")
    try:
        with db() as conn, conn.transaction(), conn.cursor() as cur:
            # lock both rows in a fixed order -> no deadlocks on A->B vs B->A
            cur.execute("select id, balance from accounts where id = any(%s) order by id for update",
                        ([body.from_account_id, body.to_account_id],))
            accts = {r["id"]: r for r in cur.fetchall()}
            for aid in (body.from_account_id, body.to_account_id):
                if aid not in accts:
                    raise ApiError(404, "ACCOUNT_NOT_FOUND", f"Account {aid} not found")
            src, dst = accts[body.from_account_id], accts[body.to_account_id]
            dup = existing_txn(cur, src["id"], body.reference_id)
            if dup:
                raise duplicate(dup)
            tid = uuid4()
            d = apply_txn(cur, src, "debit", body.amount, body.description, body.reference_id, tid)
            c = apply_txn(cur, dst, "credit", body.amount, body.description, body.reference_id, tid)
            return out({"transfer_id": tid, "debit": d, "credit": c,
                        "from_balance": src["balance"], "to_balance": dst["balance"]}, 201)
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "DUPLICATE_TRANSACTION", "reference_id already used on one of the accounts")
