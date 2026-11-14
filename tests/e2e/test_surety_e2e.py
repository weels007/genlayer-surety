"""Surety end to end on the deployed registry: the money paths around a dispute.

The Studio suite takes a claim through assert → challenge → judge. This one covers
the paths the judgement can take when nobody wins it: a stake that does not match
becomes a credit, a dispute can be withdrawn, a claimant cannot challenge their
own claim, void is refused before it is earned, a claim can be retired, and the
claimant cannot take a stake that is not theirs.

A claim is asserted by one account and disputed by another, because the contract
refuses self-challenges by design.

Nothing here deploys, and every assertion is a delta, so it can be re-run.

    pytest tests/e2e -v -s
"""

import json
import re
import sys
import time
from pathlib import Path

import pytest
from genlayer_py import create_client, studionet

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from wallet import load_account  # noqa: E402

DEPLOYMENT_PATH = ROOT / "deployment.json"
ADDRESS_VALUE = re.compile(r"0x[0-9a-fA-F]{40}")

POLL_INTERVAL = 10_000
POLL_RETRIES = 60
RETRY_PAUSE = 20

BOND = 100
WINDOW = 3600
SOURCE_URL = "https://raw.githubusercontent.com/weels007/genlayer-vow/main/README.md"
CLAIM_TEXT = "The project documents how a stranger can install and run it."

# The contract records a reference per claimant forever, so a re-run must not
# reuse the last run's references: the assertion would be refused as a duplicate
# and become a credit.
RUN = str(time.time_ns())


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _deployment() -> dict:
    if not DEPLOYMENT_PATH.exists():
        pytest.skip("no deployment.json yet; run the Deploy Contract workflow first")
    payload = json.loads(DEPLOYMENT_PATH.read_text(encoding="utf-8"))
    address = str(payload.get("address", "")).strip()
    if not ADDRESS_VALUE.fullmatch(address):
        pytest.skip(f"deployment.json has no usable address: {address!r}")
    return payload


@pytest.fixture(scope="module")
def contract() -> str:
    payload = _deployment()
    print(f"\n[surety-e2e] contract {payload['address']}")
    return payload["address"]


@pytest.fixture(scope="module")
def account():
    return load_account("tester1")


@pytest.fixture(scope="module")
def challenger(account):
    """A second funded account. Without one, disputes cannot be exercised."""
    try:
        other = load_account("tester2")
    except SystemExit:
        pytest.skip("no second wallet is configured; a challenge would be a self-challenge")
    if _hex(other.address) == _hex(account.address):
        pytest.skip("the second wallet is the deployer; a challenge would be a self-challenge")
    return other


@pytest.fixture(scope="module")
def client(account):
    return create_client(studionet, account=account)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hex(value) -> str:
    return str(value).lower().replace("0x", "")


def _succeeded(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status_name") not in ("ACCEPTED", "FINALIZED"):
        return False
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict):
        return leader.get("execution_result") == "SUCCESS"
    return str(result.get("tx_execution_result_name", "")) == "FINISHED_WITH_RETURN"


def _definite_rollback(result) -> bool:
    if not isinstance(result, dict):
        return False
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    return isinstance(leader, dict) and leader.get("execution_result") == "ERROR"


def _rollback_text(result) -> str:
    """Whatever the receipt says about why execution failed."""
    if not isinstance(result, dict):
        return str(result)

    bits = []
    leader = (result.get("consensus_data") or {}).get("leader_receipt")
    if isinstance(leader, list):
        leader = leader[0] if leader else None
    if isinstance(leader, dict):
        for key in ("message", "error_message", "error", "result"):
            value = leader.get(key)
            if isinstance(value, (str, bytes)):
                bits.append(str(value))
    for key in ("error_message", "message"):
        value = result.get(key)
        if isinstance(value, (str, bytes)):
            bits.append(str(value))
    return " ".join(part for part in bits if part)


def _attempt(client, account, contract, method, args=None, value=0):
    try:
        tx_hash = client.write_contract(
            contract, method, args=args or [], value=value, account=account
        )
        return client.wait_for_transaction_receipt(
            tx_hash, retries=POLL_RETRIES, interval=POLL_INTERVAL
        )
    except Exception as error:
        return error


def _expect_ok(client, account, contract, method, args=None, value=0, verify=None):
    result = None
    for attempt in range(3):
        result = _attempt(client, account, contract, method, args, value)
        if _succeeded(result):
            return result
        if verify is not None and verify():
            return result
        if _definite_rollback(result):
            raise AssertionError(f"{method} was refused: {result}")
        if attempt < 2:
            print(f"  {method} did not reach consensus, retrying")
            time.sleep(RETRY_PAUSE)
    raise AssertionError(f"{method} did not reach consensus: {result}")


def _expect_denied(client, account, contract, method, args=None, value=0):
    result = _attempt(client, account, contract, method, args, value)
    assert not _succeeded(result), f"{method} should have been refused: {result}"
    return result


def _read(client, contract, method, args=None):
    """Read a view, retrying a provider hiccup (a 502 is not an answer)."""
    last = None
    for attempt in range(3):
        try:
            return client.read_contract(contract, method, args=args or [])
        except Exception as error:
            last = error
            if attempt < 2:
                print(f"  {method} read failed, retrying: {error}")
                time.sleep(RETRY_PAUSE)
    raise last


def _ledger(client, contract):
    return _read(client, contract, "get_ledger")


def _balance(client, contract):
    return int(_read(client, contract, "get_balance"))


def _credit(client, contract, who):
    return int(_read(client, contract, "get_credit", [who.address]))


def _claim(client, contract, claim_id):
    return _read(client, contract, "get_claim", [claim_id])


def _claim_ids(client, contract):
    return list(_read(client, contract, "get_claim_ids").get("ids", []))


def _invariant_holds(client, contract) -> bool:
    ledger = _ledger(client, contract)
    return int(ledger["total_in"]) == int(ledger["total_out"]) + int(ledger["locked_total"])


def _settle_our_tail(client, accounts, contract):
    """Leave no dispute of ours and no credit behind before a delta is measured."""
    ours = {_hex(member.address) for member in accounts}
    for claim_id in _claim_ids(client, contract):
        record = _claim(client, contract, claim_id)
        if record["status"] != "CHALLENGED":
            continue
        if _hex(record["challenger"]) not in ours:
            pytest.skip("another account holds an open dispute")
        who = next(
            member for member in accounts if _hex(member.address) == _hex(record["challenger"])
        )
        _expect_ok(client, who, contract, "withdraw_challenge", [claim_id])

    for member in accounts:
        if _credit(client, contract, member) > 0:
            _expect_ok(client, member, contract, "withdraw_credit")


def _assert_claim(client, account, contract, ref):
    before = len(_claim_ids(client, contract))
    _expect_ok(
        client,
        account,
        contract,
        "assert_claim",
        [CLAIM_TEXT, SOURCE_URL, WINDOW, f"{ref}-{RUN}"],
        value=BOND,
        verify=lambda: len(_claim_ids(client, contract)) > before,
    )
    ids = _claim_ids(client, contract)
    assert len(ids) > before, "the claim was not created"
    return ids[-1]


def _challenged(client, challenger, contract, claim_id):
    """Challenge with the other account, and prove the dispute really opened."""
    _expect_ok(client, challenger, contract, "challenge", [claim_id], value=BOND)
    record = _claim(client, contract, claim_id)
    assert record["status"] == "CHALLENGED", (
        "the challenge did not open a dispute — a refused payable call answers "
        f"'credit' instead of reverting: {record}"
    )
    return record


# ---------------------------------------------------------------------------
# the credit path: a stake that does not match the bond
# ---------------------------------------------------------------------------


def test_01_the_registry_is_live(client, contract):
    rules = _read(client, contract, "get_rules")
    assert int(rules["min_bond"]) == BOND
    assert int(rules["max_defences"]) == 5
    assert _invariant_holds(client, contract)


def test_02_a_mismatched_stake_becomes_a_credit(
    client, account, challenger, contract
):
    _settle_our_tail(client, [account, challenger], contract)

    ledger_before = _ledger(client, contract)
    locked_before = int(ledger_before["locked_total"])

    claim_id = _assert_claim(client, account, contract, "e2e-credit")

    # A stake that does not match the bond is not a dispute: it is a credit owned
    # by the sender, withdrawable at any time.
    _expect_ok(client, challenger, contract, "challenge", [claim_id], value=BOND + 1)

    assert _claim(client, contract, claim_id)["status"] == "OPEN"
    assert _credit(client, contract, challenger) >= BOND + 1

    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == locked_before + BOND + (BOND + 1)
    assert int(ledger["total_out"]) == int(ledger_before["total_out"])
    assert _invariant_holds(client, contract)


def test_03_the_credit_comes_back(client, challenger, contract):
    locked_before = int(_ledger(client, contract)["locked_total"])
    credit_before = _credit(client, contract, challenger)
    assert credit_before >= BOND + 1

    _expect_ok(
        client,
        challenger,
        contract,
        "withdraw_credit",
        verify=lambda: _credit(client, contract, challenger) == 0,
    )

    # A payout is an external message that finalises later, so the contract's
    # real balance is not a reliable delta across runs. The ledger is.
    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == locked_before - credit_before
    assert _invariant_holds(client, contract)


# ---------------------------------------------------------------------------
# the withdrawal path: a dispute that is dropped
# ---------------------------------------------------------------------------


def test_04_a_withdrawn_dispute_refunds_the_stake(
    client, account, challenger, contract
):
    _settle_our_tail(client, [account, challenger], contract)

    locked_before = int(_ledger(client, contract)["locked_total"])
    out_before = int(_ledger(client, contract)["total_out"])

    claim_id = _assert_claim(client, account, contract, "e2e-withdraw")
    _challenged(client, challenger, contract, claim_id)

    _expect_ok(client, challenger, contract, "withdraw_challenge", [claim_id])

    record = _claim(client, contract, claim_id)
    assert record["status"] == "OPEN"
    assert record["challenged"] is False
    assert record["stake"] == 0

    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == locked_before + BOND
    assert int(ledger["total_out"]) == out_before + BOND
    assert _invariant_holds(client, contract)


def test_05_the_claimant_cannot_take_the_stake(
    client, account, challenger, contract
):
    _settle_our_tail(client, [account, challenger], contract)
    claim_id = _assert_claim(client, account, contract, "e2e-stranger")
    _challenged(client, challenger, contract, claim_id)

    # The claimant is a stranger to the dispute, exactly like anybody else.
    touched = _attempt(client, account, contract, "withdraw_challenge", [claim_id])
    assert not _succeeded(touched), "a stranger withdrew somebody else's stake"

    # A refusal only proves custody if the contract is the one refusing: an
    # unfunded wallet or a network fault looks the same from out here.
    text = _rollback_text(touched).lower()
    if text:
        assert "challenger" in text, (
            f"the stranger was stopped, but not by the contract's own refusal: {text}"
        )

    assert _claim(client, contract, claim_id)["status"] == "CHALLENGED"


def test_06_a_claimant_cannot_challenge_their_own_claim(client, account, contract):
    _settle_our_tail(client, [account], contract)
    claim_id = _assert_claim(client, account, contract, "e2e-self")

    _expect_ok(client, account, contract, "challenge", [claim_id], value=BOND)

    assert _credit(client, contract, account) >= BOND
    assert _claim(client, contract, claim_id)["status"] == "OPEN"

    _expect_ok(client, account, contract, "withdraw_credit")


# ---------------------------------------------------------------------------
# the void guard and the exit for a standing claim
# ---------------------------------------------------------------------------


def test_07_void_is_refused_before_it_is_earned(
    client, account, challenger, contract
):
    _settle_our_tail(client, [account, challenger], contract)
    claim_id = _assert_claim(client, account, contract, "e2e-void")
    _challenged(client, challenger, contract, claim_id)

    _expect_denied(client, account, contract, "void_challenge", [claim_id])
    assert _claim(client, contract, claim_id)["status"] == "CHALLENGED"

    _expect_ok(client, challenger, contract, "withdraw_challenge", [claim_id])
    assert _claim(client, contract, claim_id)["status"] == "OPEN"


def test_08_retire_returns_the_bond(client, account, contract):
    _settle_our_tail(client, [account], contract)
    claim_id = _assert_claim(client, account, contract, "e2e-retire")

    out_before = int(_ledger(client, contract)["total_out"])

    _expect_ok(
        client,
        account,
        contract,
        "retire",
        [claim_id],
        verify=lambda: _claim(client, contract, claim_id)["status"] == "RETIRED",
    )

    assert _claim(client, contract, claim_id)["status"] == "RETIRED"

    ledger = _ledger(client, contract)
    assert int(ledger["total_out"]) == out_before + BOND
    assert _invariant_holds(client, contract)


def test_09_views_agree_with_the_ledger(client, contract):
    summary = _read(client, contract, "get_summary")
    claims = _read(client, contract, "get_claims")

    assert int(summary["claims"]) == len(claims)
    assert int(summary["claims"]) == len(_claim_ids(client, contract))
    assert _balance(client, contract) >= 0
    assert _invariant_holds(client, contract)
