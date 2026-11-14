"""Challenging a claim: matching the bond, and who may do what.

A challenge is the only way to make the committee look at anything, and it costs
exactly as much as the claim is backed by. A refused challenge is a credit, not
a loss: the stake is never stranded.
"""

import pytest

CLAIM = "The project publishes a signed release."
URL = "https://source.example.com/page"
WINDOW = 86400
BOND = 500


def _deployed(direct_deploy):
    return direct_deploy("contracts/Surety.py")


def _stated(direct_vm, contract, claimant, ref="ref-1"):
    direct_vm.sender = claimant
    direct_vm.value = BOND
    claim_id = contract.assert_claim(CLAIM, URL, WINDOW, ref)
    direct_vm.value = 0
    return claim_id


def _challenge(direct_vm, contract, challenger, claim_id, value=BOND):
    direct_vm.sender = challenger
    direct_vm.value = value
    contract.challenge(claim_id)
    direct_vm.value = 0


def test_a_challenge_locks_a_matching_stake(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)

    _challenge(direct_vm, contract, direct_bob, claim_id)

    record = contract.get_claim(claim_id)
    assert record["status"] == "CHALLENGED"
    assert record["challenged"] is True
    assert record["stake"] == BOND
    assert record["attempts"] == 0
    assert record["challenger"].lower().replace("0x", "") == direct_bob.hex().lower().replace(
        "0x", ""
    )

    ledger = contract.get_ledger()
    assert ledger["total_in"] == 2 * BOND
    assert ledger["locked_total"] == 2 * BOND
    assert ledger["total_out"] == 0
    assert contract.is_challenged(claim_id) is True


def test_a_stake_that_does_not_match_the_bond_is_a_credit(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)

    _challenge(direct_vm, contract, direct_bob, claim_id, value=BOND - 1)
    _challenge(direct_vm, contract, direct_bob, claim_id, value=BOND + 1)

    assert contract.get_credit(direct_bob) == 2 * BOND
    assert contract.get_claim(claim_id)["status"] == "OPEN"
    assert contract.get_ledger()["locked_total"] == 3 * BOND


def test_nobody_challenges_their_own_claim(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)

    _challenge(direct_vm, contract, direct_alice, claim_id)

    assert contract.get_credit(direct_alice) == BOND
    assert contract.get_claim(claim_id)["status"] == "OPEN"


def test_a_zero_stake_is_a_plain_revert(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)

    direct_vm.sender = direct_bob
    direct_vm.value = 0
    with direct_vm.expect_revert("[EXPECTED]:zero_stake"):
        contract.challenge(claim_id)

    assert contract.get_claim(claim_id)["status"] == "OPEN"
    assert contract.get_credit(direct_bob) == 0


def test_an_unknown_claim_becomes_a_credit(
    direct_vm, direct_deploy, direct_bob
):
    contract = _deployed(direct_deploy)

    _challenge(direct_vm, contract, direct_bob, "su-404")

    assert contract.get_credit(direct_bob) == BOND
    assert contract.get_ledger()["locked_total"] == BOND


def test_a_settled_claim_cannot_be_challenged(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)

    direct_vm.sender = direct_alice
    contract.retire(claim_id)

    _challenge(direct_vm, contract, direct_bob, claim_id)

    assert contract.get_credit(direct_bob) == BOND
    assert contract.get_claim(claim_id)["status"] == "RETIRED"


def test_only_the_challenger_can_take_the_stake_back(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)
    _challenge(direct_vm, contract, direct_bob, claim_id)

    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("[EXPECTED]:not_challenger"):
        contract.withdraw_challenge(claim_id)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED]:not_challenger"):
        contract.withdraw_challenge(claim_id)

    assert contract.get_claim(claim_id)["status"] == "CHALLENGED"


def test_the_challenger_can_withdraw_and_the_claim_stands_again(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)
    _challenge(direct_vm, contract, direct_bob, claim_id)

    direct_vm.warp("2025-01-02T00:00:00Z")
    direct_vm.sender = direct_bob
    contract.withdraw_challenge(claim_id)

    record = contract.get_claim(claim_id)
    assert record["status"] == "OPEN"
    assert record["challenged"] is False
    assert record["stake"] == 0
    assert record["attempts"] == 0
    # The quiet window starts again: a withdrawn dispute is not a decision.
    assert record["deadline"] - record["created_ts"] >= WINDOW

    ledger = contract.get_ledger()
    assert ledger["locked_total"] == BOND
    assert ledger["total_out"] == BOND
    assert contract.get_summary()["withdrawn"] == 1


def test_a_claim_without_a_dispute_cannot_be_judged(
    direct_vm, direct_deploy, direct_alice
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED]:challenge_closed"):
        contract.judge(claim_id)


def test_a_refused_claim_must_be_refilled_before_further_challenges(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)
    _challenge(direct_vm, contract, direct_bob, claim_id)

    # A second challenger while one is pending is refused, and the stake is a
    # credit rather than a second dispute.
    _challenge(direct_vm, contract, direct_owner, claim_id)

    assert contract.get_credit(direct_owner) == BOND
    assert contract.get_claim(claim_id)["stake"] == BOND
    assert contract.get_ledger()["locked_total"] == 3 * BOND


def test_the_ledger_stays_balanced_through_a_challenge(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice)
    _challenge(direct_vm, contract, direct_bob, claim_id)

    ledger = contract.get_ledger()
    assert ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]

    with pytest.raises(Exception):
        contract.get_claim("su-404")
