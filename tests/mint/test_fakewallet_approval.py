import pytest

from cashu.core.base import Amount, MeltQuote, MeltQuoteState, Unit
from cashu.core.models import PostMeltQuoteRequest
from cashu.core.settings import settings
from cashu.lightning.base import PaymentResult
from cashu.lightning.fake import FakeWallet


@pytest.fixture
def fake_wallet():
    original_brr = settings.fakewallet_brr
    original_pay_invoice_state = settings.fakewallet_pay_invoice_state
    original_accept_arbitrary = settings.fakewallet_accept_arbitrary_melt_requests

    settings.fakewallet_brr = False
    settings.fakewallet_pay_invoice_state = PaymentResult.PENDING.name
    settings.fakewallet_accept_arbitrary_melt_requests = True

    FakeWallet.manually_approved_invoices.clear()
    FakeWallet.arbitrary_payments.clear()
    FakeWallet.created_invoices.clear()
    FakeWallet.paid_invoices_outgoing.clear()
    FakeWallet.paid_invoices_incoming.clear()
    FakeWallet.payment_secrets.clear()

    yield FakeWallet(unit=Unit.sat)

    settings.fakewallet_brr = original_brr
    settings.fakewallet_pay_invoice_state = original_pay_invoice_state
    settings.fakewallet_accept_arbitrary_melt_requests = original_accept_arbitrary

    FakeWallet.manually_approved_invoices.clear()
    FakeWallet.arbitrary_payments.clear()
    FakeWallet.created_invoices.clear()
    FakeWallet.paid_invoices_outgoing.clear()
    FakeWallet.paid_invoices_incoming.clear()
    FakeWallet.payment_secrets.clear()


def _melt_quote_for_request(request: str) -> MeltQuote:
    return MeltQuote(
        quote=f"quote-{hash(request)}",
        method="bolt11",
        request=request,
        checking_id="",
        unit="sat",
        amount=0,
        fee_reserve=0,
        state=MeltQuoteState.unpaid,
    )


@pytest.mark.asyncio
async def test_approve_invoice_basic(fake_wallet):
    response = await fake_wallet.create_invoice(Amount(Unit.sat, 100), memo="approval")
    assert response.checking_id

    pending = fake_wallet.get_pending_invoices()
    assert any(inv["payment_hash"] == response.checking_id for inv in pending)

    success = await fake_wallet.approve_invoice(response.checking_id)
    assert success is True

    status = await fake_wallet.get_invoice_status(response.checking_id)
    assert status.result == PaymentResult.SETTLED


@pytest.mark.asyncio
async def test_approve_invoice_not_found(fake_wallet):
    success = await fake_wallet.approve_invoice("nonexistent_hash")
    assert success is False


@pytest.mark.asyncio
async def test_approve_invoice_idempotent(fake_wallet):
    response = await fake_wallet.create_invoice(Amount(Unit.sat, 50), memo="idempotent")
    assert response.checking_id

    success1 = await fake_wallet.approve_invoice(response.checking_id)
    success2 = await fake_wallet.approve_invoice(response.checking_id)

    assert success1 is True
    assert success2 is True


@pytest.mark.asyncio
async def test_get_pending_invoices(fake_wallet):
    response1 = await fake_wallet.create_invoice(Amount(Unit.sat, 21), memo="inv1")
    response2 = await fake_wallet.create_invoice(Amount(Unit.sat, 34), memo="inv2")

    pending = fake_wallet.get_pending_invoices()
    pending_hashes = {inv["payment_hash"] for inv in pending}

    assert response1.checking_id in pending_hashes
    assert response2.checking_id in pending_hashes


def test_arbitrary_melt_request_parsing(fake_wallet):
    identifier, amount = fake_wallet._parse_arbitrary_request("Red:AMOUNT:100")
    assert identifier == "Red"
    assert amount == 100

    identifier, amount = fake_wallet._parse_arbitrary_request("Blue")
    assert identifier == "Blue"
    assert amount == 0

    identifier, amount = fake_wallet._parse_arbitrary_request(
        "IBAN:GB29NWBK60161331926819:AMOUNT:1000"
    )
    assert identifier == "IBAN:GB29NWBK60161331926819"
    assert amount == 1000


def test_bolt11_detection(fake_wallet):
    assert fake_wallet._is_bolt11("lnbc1000n1p3k...") is True
    assert fake_wallet._is_bolt11("LNBC1000n1p3k...") is True
    assert fake_wallet._is_bolt11("lntb1000n1p3k...") is True
    assert fake_wallet._is_bolt11("lnbcrt1000n1p3k...") is True
    assert fake_wallet._is_bolt11("Red") is False


def test_checking_id_generation(fake_wallet):
    id1 = fake_wallet._get_checking_id_for_arbitrary("Red")
    id2 = fake_wallet._get_checking_id_for_arbitrary("Red")
    id3 = fake_wallet._get_checking_id_for_arbitrary("Blue")

    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 64


@pytest.mark.asyncio
async def test_arbitrary_request_quote_and_pending_tracking(fake_wallet):
    quote_response = await fake_wallet.get_payment_quote(
        PostMeltQuoteRequest(request="Red:AMOUNT:100", unit="sat")
    )
    assert quote_response.checking_id

    payment_response = await fake_wallet.pay_invoice(
        _melt_quote_for_request("Red:AMOUNT:100"),
        fee_limit_msat=0,
    )
    assert payment_response.result == PaymentResult.PENDING

    pending = fake_wallet.get_pending_arbitrary_payments()
    assert len(pending) == 1
    assert pending[0]["identifier"] == "Red"
    assert pending[0]["amount"] == 100


@pytest.mark.asyncio
async def test_settle_arbitrary_payment(fake_wallet):
    payment_response = await fake_wallet.pay_invoice(
        _melt_quote_for_request("Blue:AMOUNT:50"),
        fee_limit_msat=0,
    )
    assert payment_response.checking_id

    status = await fake_wallet.get_payment_status(payment_response.checking_id)
    assert status.result == PaymentResult.PENDING

    success = await fake_wallet.settle_arbitrary_payment(payment_response.checking_id)
    assert success is True

    settled_status = await fake_wallet.get_payment_status(payment_response.checking_id)
    assert settled_status.result == PaymentResult.SETTLED


@pytest.mark.asyncio
async def test_reject_arbitrary_payment(fake_wallet):
    payment_response = await fake_wallet.pay_invoice(
        _melt_quote_for_request("Green:AMOUNT:75"),
        fee_limit_msat=0,
    )
    assert payment_response.checking_id

    success = await fake_wallet.reject_arbitrary_payment(payment_response.checking_id)
    assert success is True

    failed_status = await fake_wallet.get_payment_status(payment_response.checking_id)
    assert failed_status.result == PaymentResult.FAILED


@pytest.mark.asyncio
async def test_voting_workflow_totals(fake_wallet):
    vote_requests = [
        "Red:AMOUNT:10",
        "Red:AMOUNT:5",
        "Blue:AMOUNT:20",
        "Red:AMOUNT:15",
        "Blue:AMOUNT:3",
    ]

    for request in vote_requests:
        response = await fake_wallet.pay_invoice(
            _melt_quote_for_request(request),
            fee_limit_msat=0,
        )
        assert response.result == PaymentResult.PENDING

    pending = fake_wallet.get_pending_arbitrary_payments()
    red_votes = sum(p["amount"] for p in pending if p["identifier"] == "Red")
    blue_votes = sum(p["amount"] for p in pending if p["identifier"] == "Blue")

    assert red_votes == 30
    assert blue_votes == 23
    assert red_votes > blue_votes
