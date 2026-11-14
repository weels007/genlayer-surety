"""Views: what the contract admits about itself.

The quiet window is a view, not a timer: nothing settles by itself, and a claim
is only "mature" once the clock has passed its deadline.
"""

CLAIM = "The project publishes a signed release."
URL = "https://source.example.com/page"
DAY = 86400
BOND = 500


def _deployed(direct_deploy):
    return direct_deploy("contracts/Surety.py")


def _stated(direct_vm, contract, sender, ref="ref-1", value=BOND, window=DAY):
    direct_vm.sender = sender
    direct_vm.value = value
    claim_id = contract.assert_claim(CLAIM, URL, window, ref)
    direct_vm.value = 0
    return claim_id


def test_a_fresh_contract_is_empty(direct_deploy):
    contract = _deployed(direct_deploy)

    assert contract.get_claim_ids()["ids"] == []
    assert contract.get_claims() == {}
    assert contract.get_ledger() == {"total_in": 0, "total_out": 0, "locked_total": 0}
    assert contract.get_summary() == {
        "claims": 0,
        "upheld": 0,
        "refuted": 0,
        "retired": 0,
        "withdrawn": 0,
        "void": 0,
    }


def test_claims_are_listed_in_order(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deployed(direct_deploy)

    _stated(direct_vm, contract, direct_alice, ref="a")
    _stated(direct_vm, contract, direct_bob, ref="b")

    assert contract.get_claim_ids()["ids"] == ["su-1", "su-2"]

    claims = contract.get_claims()
    assert len(claims) == 2
    assert claims["su-1"]["text"] == CLAIM
    assert claims["su-2"]["status"] == "OPEN"


def test_a_claim_is_not_mature_before_its_window_passes(
    direct_vm, direct_deploy, direct_alice
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = _deployed(direct_deploy)

    claim_id = _stated(direct_vm, contract, direct_alice, window=3600)

    assert contract.is_mature(claim_id) is False

    direct_vm.warp("2025-01-01T00:30:00Z")
    assert contract.is_mature(claim_id) is False

    direct_vm.warp("2025-01-01T02:00:00Z")
    assert contract.is_mature(claim_id) is True


def test_a_disputed_claim_is_never_mature(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    direct_vm.warp("2025-01-01T00:00:00Z")
    contract = _deployed(direct_deploy)
    claim_id = _stated(direct_vm, contract, direct_alice, window=3600)

    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    contract.challenge(claim_id)
    direct_vm.value = 0

    direct_vm.warp("2025-01-05T00:00:00Z")
    assert contract.is_mature(claim_id) is False
    assert contract.is_challenged(claim_id) is True


def test_unknown_claims_are_never_mature(direct_deploy):
    contract = _deployed(direct_deploy)

    assert contract.is_mature("su-404") is False
    assert contract.is_challenged("su-404") is False


def test_the_summary_counts_every_outcome(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = _deployed(direct_deploy)

    retired = _stated(direct_vm, contract, direct_alice, ref="retired")
    direct_vm.sender = direct_alice
    contract.retire(retired)

    _stated(direct_vm, contract, direct_alice, ref="standing")

    summary = contract.get_summary()
    assert summary["claims"] == 2
    assert summary["retired"] == 1
    assert summary["upheld"] == 0
    assert summary["refuted"] == 0

    ledger = contract.get_ledger()
    assert ledger["total_in"] == 2 * BOND
    assert ledger["total_out"] == BOND
    assert ledger["locked_total"] == BOND
    assert ledger["total_in"] == ledger["total_out"] + ledger["locked_total"]
