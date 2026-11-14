"""The judgement: what the page says, what it costs to disagree, and what happens
when it says nothing.

Only a dispute ever reaches the model. A verdict without a quote taken from the
page is not a verdict — it becomes SILENT, books an attempt, and moves no money,
so an ungrounded citation can never settle somebody's claim.
"""

import json

import pytest

PAGE = (
    "Surety is a bonded claim registry for GenLayer. It stores each claim with the "
    "bond that backs it, and only calls the committee when someone disputes it. "
    "The repository is public and every settled claim keeps the quote that decided it."
)
QUOTE = "It stores each claim with the bond that backs it."
CLAIM = "The project publishes a signed release."
URL = "https://source.example.com/page"
WINDOW = 86400
BOND = 500


def _mock_page(direct_vm, body=PAGE):
    direct_vm.mock_web(r".*source\.example\.com.*", {"status": 200, "body": body})


def _mock_judge(direct_vm, verdict="SUPPORTS", quote=QUOTE, reason="the page says so"):
    direct_vm.mock_llm(
        r".*\[\[SURETY_JUDGEMENT\]\].*",
        json.dumps({"verdict": verdict, "quote": quote, "reason": reason}),
    )


def _disputed(direct_vm, direct_deploy, claimant, challenger):
    contract = direct_deploy("contracts/Surety.py")

    direct_vm.sender = claimant
    direct_vm.value = BOND
    claim_id = contract.assert_claim(CLAIM, URL, WINDOW, "ref-1")
    direct_vm.value = 0

    direct_vm.sender = challenger
    direct_vm.value = BOND
    contract.challenge(claim_id)
    direct_vm.value = 0

    return contract, claim_id


def test_a_supported_claim_keeps_the_stake_and_doubles(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "SUPPORTS")

    direct_vm.sender = direct_alice
    assert contract.judge(claim_id) == "SUPPORTS"

    record = contract.get_claim(claim_id)
    assert record["status"] == "OPEN"
    assert record["challenged"] is False
    assert record["stake"] == 0
    assert record["bond"] == 2 * BOND
    assert record["rounds"] == 1
    assert record["attempts"] == 1
    assert record["quote"].lower() == QUOTE.lower()
    assert record["page_digest"] != ""

    # The pot doubled, so the money stayed inside the contract, still locked.
    ledger = contract.get_ledger()
    assert ledger["total_in"] == 2 * BOND
    assert ledger["locked_total"] == 2 * BOND
    assert ledger["total_out"] == 0
    assert ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]
    assert contract.get_summary()["upheld"] == 1


def test_a_contradicted_claim_pays_the_challenger(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "CONTRADICTS", quote="The repository is public")

    direct_vm.sender = direct_alice
    assert contract.judge(claim_id) == "CONTRADICTS"

    record = contract.get_claim(claim_id)
    assert record["status"] == "REFUTED"
    assert record["stake"] == 0
    assert record["bond"] == BOND
    assert contract.get_summary()["refuted"] == 1

    # The challenger takes the whole pot: their stake back plus the bond.
    ledger = contract.get_ledger()
    assert ledger["total_out"] == 2 * BOND
    assert ledger["locked_total"] == 0
    assert ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]


def test_a_silent_page_decides_nothing(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "SILENT", quote="The repository is public")

    direct_vm.sender = direct_alice
    assert contract.judge(claim_id) == "SILENT"

    record = contract.get_claim(claim_id)
    assert record["status"] == "CHALLENGED"
    assert record["attempts"] == 1
    assert record["bond"] == BOND
    assert record["stake"] == BOND

    ledger = contract.get_ledger()
    assert ledger["locked_total"] == 2 * BOND
    assert ledger["total_out"] == 0


def test_an_ungrounded_quote_is_not_a_verdict(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "CONTRADICTS", quote="a sentence that is not on the page at all")

    direct_vm.sender = direct_alice
    assert contract.judge(claim_id) == "SILENT"

    record = contract.get_claim(claim_id)
    assert record["status"] == "CHALLENGED"
    assert record["attempts"] == 1
    assert record["quote"] == ""
    # The money did not move on an unproven verdict.
    assert contract.get_ledger()["total_out"] == 0


def test_an_empty_page_is_an_external_error(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm, body="")
    _mock_judge(direct_vm, "SUPPORTS")

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXTERNAL]:empty_source_page"):
        contract.judge(claim_id)

    # A failed read books nothing: the attempt counter is untouched.
    assert contract.get_claim(claim_id)["attempts"] == 0


def test_the_attempts_run_out_and_anyone_may_void(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "SILENT", quote="The repository is public")

    direct_vm.sender = direct_alice
    for _ in range(3):
        assert contract.judge(claim_id) == "SILENT"

    assert contract.get_claim(claim_id)["attempts"] == 3

    with direct_vm.expect_revert("[EXPECTED]:too_many_attempts"):
        contract.judge(claim_id)

    # Anyone may refund a dispute the committee could not decide, and the stake
    # goes to the challenger even when a stranger calls.
    direct_vm.sender = direct_owner
    contract.void_challenge(claim_id)

    record = contract.get_claim(claim_id)
    assert record["status"] == "OPEN"
    assert record["challenged"] is False
    assert record["stake"] == 0
    assert record["attempts"] == 0
    assert contract.get_summary()["void"] == 1

    ledger = contract.get_ledger()
    assert ledger["total_out"] == BOND
    assert ledger["locked_total"] == BOND
    assert ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]


def test_void_is_refused_before_the_attempts_are_spent(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("[EXPECTED]:not_voidable"):
        contract.void_challenge(claim_id)


def test_each_defence_doubles_the_price_of_disagreement(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "SUPPORTS")

    direct_vm.sender = direct_alice
    contract.judge(claim_id)

    assert contract.get_claim(claim_id)["bond"] == 2 * BOND

    # The next challenger has to match the doubled pot.
    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    contract.challenge(claim_id)
    direct_vm.value = 0

    assert contract.get_credit(direct_bob) == BOND
    assert contract.get_claim(claim_id)["status"] == "OPEN"

    direct_vm.sender = direct_bob
    direct_vm.value = 2 * BOND
    contract.challenge(claim_id)
    direct_vm.value = 0

    record = contract.get_claim(claim_id)
    assert record["status"] == "CHALLENGED"
    assert record["stake"] == 2 * BOND

    # The bond (now 2B), the mismatched stake held as a credit (B), and the
    # matched stake (2B) are all still inside the contract.
    assert contract.get_ledger()["locked_total"] == 5 * BOND


def test_a_claim_becomes_final_after_enough_defences(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "SUPPORTS")

    stake = BOND
    direct_vm.sender = direct_alice
    for round_index in range(5):
        assert contract.judge(claim_id) == "SUPPORTS"

        record = contract.get_claim(claim_id)
        assert record["rounds"] == round_index + 1
        assert record["bond"] == BOND * (2 ** (round_index + 1))

        if round_index == 4:
            break

        stake = int(record["bond"])
        direct_vm.sender = direct_bob
        direct_vm.value = stake
        contract.challenge(claim_id)
        direct_vm.value = 0

    # Five defences is the end of the line: the claim is final, and a challenge
    # is refused with a credit rather than a stake that cannot win.
    direct_vm.sender = direct_bob
    direct_vm.value = 32 * BOND
    contract.challenge(claim_id)
    direct_vm.value = 0

    assert contract.get_credit(direct_bob) == 32 * BOND
    assert contract.get_claim(claim_id)["status"] == "OPEN"
    assert contract.get_claim(claim_id)["rounds"] == 5


def test_the_ledger_stays_balanced_through_a_full_round(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract, claim_id = _disputed(direct_vm, direct_deploy, direct_alice, direct_bob)

    _mock_page(direct_vm)
    _mock_judge(direct_vm, "SUPPORTS")
    direct_vm.sender = direct_alice
    contract.judge(claim_id)

    direct_vm.sender = direct_bob
    direct_vm.value = 2 * BOND
    contract.challenge(claim_id)
    direct_vm.value = 0

    direct_vm.clear_mocks()
    _mock_page(direct_vm)
    _mock_judge(direct_vm, "CONTRADICTS", quote="The repository is public")
    direct_vm.sender = direct_alice
    contract.judge(claim_id)

    ledger = contract.get_ledger()
    assert ledger["total_in"] == 2 * BOND + 2 * BOND
    assert ledger["total_out"] == 4 * BOND
    assert ledger["locked_total"] == 0
    assert ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]

    with pytest.raises(Exception):
        contract.get_claim("su-404")
