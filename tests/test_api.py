"""Run against a real Postgres (use a test DB, not production):
    export DATABASE_URL=postgresql://...
    pytest -v
"""
import threading
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from main import app

# Create a client application
client = TestClient(app)

# new test account creation 
def new_account(name="Test"):
    r = client.post("/accounts", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def txn(acc, type_, amount, ref=None, desc="d"):
    return client.post(f"/accounts/{acc}/transactions",
                       json={"type": type_, "amount": amount, "description": desc,
                             "reference_id": ref or str(uuid.uuid4())})


def bal(acc):
    return Decimal(client.get(f"/accounts/{acc}/balance").json()["balance"])


# ---- accounts ----
def test_create_account_starts_at_zero():
    r = client.post("/accounts", json={"name": "Esha"})
    assert r.status_code == 201
    body = r.json()
    assert body["balance"] == "0.00" and body["created_at"] and body["id"]


def test_client_cannot_set_balance():
    assert client.post("/accounts", json={"name": "X", "balance": 500}).status_code == 422


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "   "}])
def test_name_required(payload):
    r = client.post("/accounts", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# ---- balance ----
def test_balance_unknown_account_404():
    r = client.get(f"/accounts/{uuid.uuid4()}/balance")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_invalid_account_id_format():
    assert client.get("/accounts/not-a-uuid/balance").status_code == 422


# ---- transactions ----
def test_spec_example_balance_100():
    a = new_account()
    for t, amt in [("credit", 100), ("credit", 50), ("debit", 30), ("debit", 20)]:
        assert txn(a, t, amt).status_code == 201
    assert bal(a) == Decimal("100.00")


def test_debit_insufficient_rejected_balance_unchanged():
    a = new_account()
    txn(a, "credit", 100)
    r = txn(a, "debit", 150)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert bal(a) == Decimal("100.00")
    hist = client.get(f"/accounts/{a}/transactions").json()
    assert hist["pagination"]["total_records"] == 1  # rejected txn not stored


def test_txn_unknown_account_404():
    assert txn(str(uuid.uuid4()), "credit", 10).status_code == 404


@pytest.mark.parametrize("amount", [0, -5, "abc", 10.123, None])
def test_invalid_amount(amount):
    a = new_account()
    assert txn(a, "credit", amount).status_code == 422


def test_invalid_type_and_missing_reference():
    a = new_account()
    assert txn(a, "refund", 10).status_code == 422
    r = client.post(f"/accounts/{a}/transactions", json={"type": "credit", "amount": 10})
    assert r.status_code == 422


def test_duplicate_reference_same_account():
    a = new_account()
    assert txn(a, "credit", 100, ref="R1").status_code == 201
    r = txn(a, "credit", 100, ref="R1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_TRANSACTION"
    assert bal(a) == Decimal("100.00")
    assert client.get(f"/accounts/{a}/transactions").json()["pagination"]["total_records"] == 1


def test_same_reference_different_accounts_ok():
    a, b = new_account(), new_account()
    assert txn(a, "credit", 10, ref="SHARED").status_code == 201
    assert txn(b, "credit", 10, ref="SHARED").status_code == 201


def test_transactions_immutable_via_api():
    a = new_account()
    t = txn(a, "credit", 10).json()["transaction"]["id"]
    for m in ("put", "patch", "delete"):
        r = getattr(client, m)(f"/accounts/{a}/transactions/{t}")
        assert r.status_code in (404, 405)


# ---- history ----
def test_history_filter_sort_pagination():
    a = new_account()
    txn(a, "credit", 100, ref="c1")
    txn(a, "credit", 100, ref="c2")
    txn(a, "debit", 10, ref="d1")
    h = client.get(f"/accounts/{a}/transactions").json()
    assert [t["reference_id"] for t in h["data"]] == ["d1", "c2", "c1"]  # latest first
    assert h["pagination"] == {"page": 1, "limit": 20, "total_records": 3, "total_pages": 1}

    h = client.get(f"/accounts/{a}/transactions?type=credit").json()
    assert h["pagination"]["total_records"] == 2 and all(t["type"] == "credit" for t in h["data"])

    h = client.get(f"/accounts/{a}/transactions?limit=2&page=2").json()
    assert len(h["data"]) == 1 and h["pagination"]["total_pages"] == 2


@pytest.mark.parametrize("qs", ["page=0", "limit=0", "limit=101", "page=abc", "type=bogus"])
def test_invalid_query_params(qs):
    a = new_account()
    assert client.get(f"/accounts/{a}/transactions?{qs}").status_code == 422


def test_history_unknown_account_404():
    assert client.get(f"/accounts/{uuid.uuid4()}/transactions").status_code == 404


# ---- concurrency ----
def test_concurrent_debits_never_overspend():
    a = new_account()
    txn(a, "credit", 100)
    results = []

    def worker(i):
        results.append(txn(a, "debit", 30, ref=f"c{i}").status_code)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(201) == 3  # 100 // 30
    assert bal(a) == Decimal("10.00")


def test_concurrent_duplicate_reference_creates_one():
    a = new_account()
    results = []
    ts = [threading.Thread(target=lambda: results.append(txn(a, "credit", 10, ref="SAME").status_code))
          for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(201) == 1 and bal(a) == Decimal("10.00")


# ---- transfers ----
def transfer(src, dst, amount, ref=None):
    return client.post("/transfers", json={"from_account_id": src, "to_account_id": dst,
                                           "amount": amount, "reference_id": ref or str(uuid.uuid4())})


def test_transfer_success():
    a, b = new_account(), new_account()
    txn(a, "credit", 100); txn(b, "credit", 50)
    assert transfer(a, b, 30).status_code == 201
    assert bal(a) == Decimal("70.00") and bal(b) == Decimal("80.00")


def test_transfer_insufficient_changes_nothing():
    a, b = new_account(), new_account()
    txn(a, "credit", 10)
    assert transfer(a, b, 30).status_code == 422
    assert bal(a) == Decimal("10.00") and bal(b) == Decimal("0.00")


def test_transfer_missing_account_changes_nothing():
    a = new_account()
    txn(a, "credit", 100)
    assert transfer(a, str(uuid.uuid4()), 30).status_code == 404
    assert bal(a) == Decimal("100.00")


def test_transfer_same_account_rejected():
    a = new_account()
    assert transfer(a, a, 10).status_code == 422


def test_transfer_duplicate_reference():
    a, b = new_account(), new_account()
    txn(a, "credit", 100)
    assert transfer(a, b, 30, ref="T1").status_code == 201
    assert transfer(a, b, 30, ref="T1").status_code == 409
    assert bal(a) == Decimal("70.00")
