"""Surety on studionet: a bond, a dispute and a real judgement.

The contract has no owner and no reset, so every assertion is a delta — a count,
a balance, a ledger total compared before and after — and the suite tolerates
claims and credits left behind by an earlier run.

A claim is asserted by one account and disputed by another, because the contract
refuses to let a claimant challenge their own claim; the two roles cannot be
played by the same wallet, even in a test.

The claim in this suite is about a page that really exists, so the live committee
has something concrete to read. Its verdict may go any of three ways, and each
one has a law attached: a supported claim keeps the stake and doubles, a
contradicted one is paid out to the challenger, and a page that says nothing
decides nothing.

    pytest tests/integration -v -s
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
def deployment() -> dict:
    return _deployment()


@pytest.fixture(scope="module")
def contract(deployment) -> str:
    print(f"\n[surety] contract {deployment['address']}")
    return deployment["address"]


@pytest.fixture(scope="module")
def account():
    return load_account("tester1")


@pytest.fixture(scope="module")
def challenger(account):
    """A second funded account. Without one, disputes cannot be exercised.

    The contract refuses a claimant's own challenge and turns it into a credit,
    so a suite that challenged with the claimant's wallet would prove nothing.
    """
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


def _wait_balance(client, contract, expected, attempts=30, pause=10):
    for _ in range(attempts):
        if _balance(client, contract) == expected:
            return expected
        time.sleep(pause)
    return _balance(client, contract)


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


def _assert_claim(client, account, contract, ref, text=CLAIM_TEXT, url=SOURCE_URL):
    before = len(_claim_ids(client, contract))
    _expect_ok(
        client,
        account,
        contract,
        "assert_claim",
        [text, url, WINDOW, ref],
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
# the registry is live
# ---------------------------------------------------------------------------


def test_01_the_registry_is_live(client, contract):
    rules = _read(client, contract, "get_rules")
    assert int(rules["min_bond"]) == BOND
    assert int(rules["max_defences"]) == 5
    assert int(rules["max_judgement_attempts"]) == 3

    assert _invariant_holds(client, contract)


# ---------------------------------------------------------------------------
# a claim, a dispute, and a real judgement
# ---------------------------------------------------------------------------


def test_02_a_claim_locks_its_bond(client, account, contract):
    _settle_our_tail(client, [account], contract)

    ledger_before = _ledger(client, contract)
    claim_id = _assert_claim(client, account, contract, "live-1")

    record = _claim(client, contract, claim_id)
    assert record["status"] == "OPEN"
    assert int(record["bond"]) == BOND
    assert int(record["stake"]) == 0
    assert record["text"] == CLAIM_TEXT

    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == int(ledger_before["locked_total"]) + BOND
    assert int(ledger["total_in"]) == int(ledger_before["total_in"]) + BOND
    assert _invariant_holds(client, contract)


def test_03_a_challenge_matches_the_bond(client, account, challenger, contract):
    _settle_our_tail(client, [account, challenger], contract)
    claim_id = _assert_claim(client, account, contract, "live-2")

    # A stake that does not match is a credit, not a dispute.
    _expect_ok(client, challenger, contract, "challenge", [claim_id], value=BOND - 1)
    assert _credit(client, contract, challenger) >= BOND - 1
    assert _claim(client, contract, claim_id)["status"] == "OPEN"

    ledger_before = _ledger(client, contract)
    record = _challenged(client, challenger, contract, claim_id)

    assert record["challenged"] is True
    assert int(record["stake"]) == BOND
    assert _hex(record["challenger"]) == _hex(challenger.address)

    ledger = _ledger(client, contract)
    assert int(ledger["locked_total"]) == int(ledger_before["locked_total"]) + BOND
    assert _invariant_holds(client, contract)


def test_04_the_committee_reads_the_page_once(client, account, challenger, contract):
    _settle_our_tail(client, [account, challenger], contract)
    claim_id = _assert_claim(client, account, contract, "live-3")
    _challenged(client, challenger, contract, claim_id)

    ledger_before = _ledger(client, contract)
    balance_before = _balance(client, contract)

    _expect_ok(
        client,
        account,
        contract,
        "judge",
        [claim_id],
        verify=lambda: int(_claim(client, contract, claim_id)["attempts"]) > 0,
    )

    record = _claim(client, contract, claim_id)
    assert int(record["attempts"]) == 1
    verdict = record["status"]
    print(f"  the committee returned {verdict!r} for the live claim")

    if verdict == "CHALLENGED":
        # SILENT: the page did not decide it, so nothing moved.
        assert record["quote"] == ""
        ledger = _ledger(client, contract)
        assert int(ledger["locked_total"]) == int(ledger_before["locked_total"])
        assert int(ledger["total_out"]) == int(ledger_before["total_out"])

        # The challenger can always take the stake back.
        _expect_ok(client, challenger, contract, "withdraw_challenge", [claim_id])
        assert _wait_balance(client, contract, balance_before) == balance_before
    elif verdict == "OPEN":
        # SUPPORTS: the claim kept the stake, so its bond doubled.
        assert record["quote"], "a supported claim must keep the quote that decided it"
        assert int(record["bond"]) == 2 * BOND
        assert int(record["rounds"]) == 1
        ledger = _ledger(client, contract)
        assert int(ledger["locked_total"]) == int(ledger_before["locked_total"])
        assert int(ledger["total_out"]) == int(ledger_before["total_out"])
    else:
        # CONTRADICTS: the challenger took the whole pot.
        assert verdict == "REFUTED", verdict
        assert record["quote"], "a contradicted claim must keep the quote that refuted it"
        ledger = _ledger(client, contract)
        assert int(ledger["total_out"]) == int(ledger_before["total_out"]) + 2 * BOND
        assert int(ledger["locked_total"]) == int(ledger_before["locked_total"]) - 2 * BOND
        assert _wait_balance(client, contract, balance_before) == balance_before

    assert _invariant_holds(client, contract)


# ---------------------------------------------------------------------------
# adversarial: the boundaries of the money
# ---------------------------------------------------------------------------


def test_05_a_stranger_cannot_take_the_stake(client, account, challenger, contract):
    _settle_our_tail(client, [account, challenger], contract)
    claim_id = _assert_claim(client, account, contract, "live-4")
    _challenged(client, challenger, contract, claim_id)

    # The claimant is a stranger to the dispute: only the challenger may take
    # the stake back.
    touched = _attempt(client, account, contract, "withdraw_challenge", [claim_id])
    assert not _succeeded(touched), "a stranger withdrew somebody else's stake"
    assert _claim(client, contract, claim_id)["status"] == "CHALLENGED"

    _expect_ok(client, challenger, contract, "withdraw_challenge", [claim_id])
    assert _claim(client, contract, claim_id)["status"] == "OPEN"


def test_06_a_claimant_cannot_challenge_their_own_claim(client, account, contract):
    _settle_our_tail(client, [account], contract)
    claim_id = _assert_claim(client, account, contract, "live-5")

    _expect_ok(client, account, contract, "challenge", [claim_id], value=BOND)

    assert _credit(client, contract, account) >= BOND
    assert _claim(client, contract, claim_id)["status"] == "OPEN"

    _expect_ok(client, account, contract, "withdraw_credit")


def test_07_void_is_refused_before_it_is_earned(client, account, challenger, contract):
    _settle_our_tail(client, [account, challenger], contract)
    claim_id = _assert_claim(client, account, contract, "live-6")
    _challenged(client, challenger, contract, claim_id)

    _expect_denied(client, account, contract, "void_challenge", [claim_id])
    assert _claim(client, contract, claim_id)["status"] == "CHALLENGED"

    balance_before = _balance(client, contract)
    _expect_ok(client, challenger, contract, "withdraw_challenge", [claim_id])
    assert _wait_balance(client, contract, balance_before - BOND) == balance_before - BOND
    assert _invariant_holds(client, contract)


def test_08_retire_returns_the_bond(client, account, contract):
    _settle_our_tail(client, [account], contract)
    claim_id = _assert_claim(client, account, contract, "live-7")

    balance_before = _balance(client, contract)
    _expect_ok(
        client,
        account,
        contract,
        "retire",
        [claim_id],
        verify=lambda: _claim(client, contract, claim_id)["status"] == "RETIRED",
    )

    assert _claim(client, contract, claim_id)["status"] == "RETIRED"
    assert _wait_balance(client, contract, balance_before - BOND) == balance_before - BOND
    assert _invariant_holds(client, contract)


def test_09_views_agree_with_the_ledger(client, contract):
    summary = _read(client, contract, "get_summary")
    claims = _read(client, contract, "get_claims")

    assert int(summary["claims"]) == len(claims)
    assert int(summary["claims"]) == len(_claim_ids(client, contract))
    assert _balance(client, contract) >= 0
    assert _invariant_holds(client, contract)
