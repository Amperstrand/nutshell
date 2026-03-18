import asyncio
import hashlib
import math
from datetime import datetime
from os import urandom
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from loguru import logger

from bolt11 import (
    Bolt11,
    Feature,
    Features,
    FeatureState,
    MilliSatoshi,
    TagChar,
    Tags,
    decode,
    encode,
)

from ..core.base import Amount, MeltQuote, Unit
from ..core.helpers import fee_reserve
from ..core.models import PostMeltQuoteRequest
from ..core.settings import settings
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
)


class FakeWallet(LightningBackend):
    """Fake wallet for testing without Lightning infrastructure.

    Supports two modes for both minting and melting:

    Minting (incoming):
    - Auto-pay (FAKEWALLET_BRR=True): All invoices automatically marked as paid
    - Manual approval (FAKEWALLET_BRR=False): Call approve_invoice() to approve,
      or use gRPC UpdateNut04Quote to set quote state to "paid"

    Melting (outgoing):
    - Auto-melt (FAKEWALLET_BRR=True): All payments automatically succeed
    - Manual approval (FAKEWALLET_BRR=False + FAKEWALLET_PAY_INVOICE_STATE=PENDING):
      Payments stay pending until approved via gRPC UpdateNut05Quote

    Arbitrary Melt Requests:
    - With FAKEWALLET_ACCEPT_ARBITRARY_MELT_REQUESTS=True, accepts non-bolt11
      strings like "IBAN:GB29NWBK..." for melt operations.
    - Format: "METHOD:identifier:AMOUNT:sats" (amount is optional)
    - Example: "IBAN:GB29NWBK60161331926819:AMOUNT:1000"
    """

    unit: Unit
    fake_btc_price = 1e8 / 1337
    paid_invoices_queue: asyncio.Queue[Bolt11] = asyncio.Queue(0)
    payment_secrets: Dict[str, str] = dict()
    created_invoices: List[Bolt11] = []
    paid_invoices_outgoing: List[Bolt11] = []
    paid_invoices_incoming: List[Bolt11] = []
    secret: str = "FAKEWALLET SECRET"
    privkey: str = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode(),
        b"FakeWallet",
        2048,
        32,
    ).hex()

    # Manual approval tracking
    manually_approved_invoices: set = set()  # payment_hashes approved for minting
    arbitrary_payments: Dict[str, dict] = {}  # checking_id -> {request, amount, status}

    supported_units = {Unit.sat, Unit.msat, Unit.usd, Unit.eur}
    balance: Dict[Unit, Amount] = {
        Unit.sat: Amount(Unit.sat, settings.fakewallet_balance_sat),
        Unit.msat: Amount(Unit.msat, settings.fakewallet_balance_sat * 1000),
        Unit.usd: Amount(Unit.usd, settings.fakewallet_balance_usd),
        Unit.eur: Amount(Unit.eur, settings.fakewallet_balance_eur),
    }

    supports_incoming_payment_stream: bool = True
    supports_description: bool = True

    def __init__(self, unit: Unit = Unit.sat, **kwargs):
        self.assert_unit_supported(unit)
        self.unit = unit

    async def status(self) -> StatusResponse:
        return StatusResponse(
            error_message=None,
            balance=Amount(self.unit, self.balance[self.unit].amount),
        )

    async def mark_invoice_paid(self, invoice: Bolt11, delay=True) -> None:
        if invoice in self.paid_invoices_incoming:
            return
        if not settings.fakewallet_brr:
            return
        if settings.fakewallet_delay_incoming_payment and delay:
            await asyncio.sleep(settings.fakewallet_delay_incoming_payment)
        self.paid_invoices_incoming.append(invoice)
        await self.paid_invoices_queue.put(invoice)
        self.update_balance(invoice, incoming=True)

    def update_balance(self, invoice: Bolt11, incoming: bool) -> None:
        amount_bolt11 = invoice.amount_msat
        assert amount_bolt11, "invoice has no amount."
        amount = int(amount_bolt11)
        if self.unit == Unit.sat:
            amount = amount // 1000
        elif self.unit == Unit.usd or self.unit == Unit.eur:
            amount = math.ceil(amount / 1e9 * self.fake_btc_price)
        elif self.unit == Unit.msat:
            amount = amount
        else:
            raise NotImplementedError()

        if incoming:
            self.balance[self.unit] += Amount(self.unit, amount)
        else:
            self.balance[self.unit] -= Amount(self.unit, amount)

    async def approve_invoice(self, payment_hash: str) -> bool:
        """Manually approve a pending invoice for minting.

        When FAKEWALLET_BRR=False, invoices remain unpaid until explicitly
        approved via this method. This is useful for testing approval
        workflows in minting operations.

        Args:
            payment_hash: Payment hash of the invoice to approve

        Returns:
            True if invoice was found and approved, False otherwise
        """
        # Find the invoice
        invoice = next(
            (i for i in self.created_invoices if i.payment_hash == payment_hash),
            None
        )
        if not invoice:
            logger.warning(f"Invoice {payment_hash} not found for approval")
            return False

        # Avoid duplicate approval
        if payment_hash in self.manually_approved_invoices:
            logger.debug(f"Invoice {payment_hash} already approved")
            return True

        # Mark as approved
        self.manually_approved_invoices.add(payment_hash)

        # Trigger the normal payment flow
        if invoice not in self.paid_invoices_incoming:
            self.paid_invoices_incoming.append(invoice)
            await self.paid_invoices_queue.put(invoice)
            self.update_balance(invoice, incoming=True)

        logger.info(f"Manually approved invoice {payment_hash}")
        return True

    def get_pending_invoices(self) -> List[Dict]:
        """Get all invoices awaiting approval.

        Returns:
            List of dicts with payment_hash, amount_msat, and created timestamp
        """
        return [
            {
                "payment_hash": inv.payment_hash,
                "amount_msat": int(inv.amount_msat) if inv.amount_msat else 0,
                "created": inv.date,
            }
            for inv in self.created_invoices
            if inv.payment_hash not in self.manually_approved_invoices
            and inv not in self.paid_invoices_incoming
        ]

    def _parse_arbitrary_request(self, request: str) -> Tuple[str, int]:
        """Parse arbitrary request string to extract identifier and amount.

        Supported formats:
        - "METHOD:identifier:AMOUNT:sats" - explicit amount
        - "METHOD:identifier" - no amount (returns 0)

        Examples:
        - "IBAN:GB29NWBK60161331926819:AMOUNT:1000" -> ("IBAN:GB29...", 1000)
        - "IBAN:GB29NWBK60161331926819" -> ("IBAN:GB29...", 0)
        - "BANK:US123456789:AMOUNT:5000" -> ("BANK:US123456789", 5000)

        Args:
            request: Arbitrary request string

        Returns:
            Tuple of (identifier, amount_sat)
        """
        # Check for AMOUNT suffix
        parts = request.rsplit(":AMOUNT:", 1)
        if len(parts) == 2:
            identifier = parts[0]
            try:
                amount = int(parts[1])
                return identifier, amount
            except ValueError:
                logger.warning(f"Invalid amount in request: {parts[1]}")
                return request, 0

        # No amount specified
        return request, 0

    def _is_bolt11(self, request: str) -> bool:
        """Check if request looks like a bolt11 invoice.

        Args:
            request: Request string to check

        Returns:
            True if it looks like a bolt11 invoice
        """
        # Bolt11 invoices start with lnbc, lntb, or lnbcrt
        return request.lower().startswith(("lnbc", "lntb", "lnbcrt"))

    def _get_checking_id_for_arbitrary(self, request: str) -> str:
        """Generate a checking_id for an arbitrary request.

        Uses SHA256 hash of the request string.

        Args:
            request: Arbitrary request string

        Returns:
            64-character hex string as checking_id
        """
        return hashlib.sha256(request.encode()).hexdigest()

    def create_dummy_bolt11(self, payment_hash: str) -> Bolt11:
        tags = Tags()
        tags.add(TagChar.payment_hash, payment_hash)
        tags.add(TagChar.payment_secret, urandom(32).hex())
        return Bolt11(
            currency="bc",
            amount_msat=MilliSatoshi(1337),
            date=int(datetime.now().timestamp()),
            tags=tags,
        )

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        expiry: Optional[int] = None,
        payment_secret: Optional[bytes] = None,
    ) -> InvoiceResponse:
        self.assert_unit_supported(amount.unit)
        tags = Tags()
        tags.add(
            TagChar.features,
            Features.from_feature_list(
                {Feature.payment_secret: FeatureState.supported}
            ),
        )

        if description_hash:
            tags.add(TagChar.description_hash, description_hash.hex())
        elif unhashed_description:
            tags.add(
                TagChar.description_hash,
                hashlib.sha256(unhashed_description).hexdigest(),
            )
        else:
            tags.add(TagChar.description, memo or "")

        tags.add(TagChar.expire_time, expiry or 3600)

        if payment_secret:
            secret = payment_secret.hex()
        else:
            secret = urandom(32).hex()
        tags.add(TagChar.payment_secret, secret)

        payment_hash = hashlib.sha256(secret.encode()).hexdigest()

        tags.add(TagChar.payment_hash, payment_hash)

        self.payment_secrets[payment_hash] = secret

        amount_msat = 0
        if self.unit == Unit.sat:
            amount_msat = MilliSatoshi(amount.to(Unit.msat, round="up").amount)
        elif self.unit == Unit.msat:
            amount_msat = MilliSatoshi(amount.amount)
        elif self.unit == Unit.usd or self.unit == Unit.eur:
            amount_msat = MilliSatoshi(
                math.ceil(amount.amount / self.fake_btc_price * 1e9)
            )
        else:
            raise NotImplementedError()

        bolt11 = Bolt11(
            currency="bc",
            amount_msat=amount_msat,
            date=int(datetime.now().timestamp()),
            tags=tags,
        )

        if bolt11 not in self.created_invoices:
            self.created_invoices.append(bolt11)
        else:
            raise ValueError("Invoice already created")

        payment_request = encode(bolt11, self.privkey)

        if settings.fakewallet_brr:
            asyncio.create_task(self.mark_invoice_paid(bolt11))

        return InvoiceResponse(
            ok=True, checking_id=payment_hash, payment_request=payment_request
        )

    async def pay_invoice(self, quote: MeltQuote, fee_limit: int) -> PaymentResponse:
        if settings.fakewallet_pay_invoice_state_exception:
            raise Exception("FakeWallet pay_invoice exception")

        # Try to decode as bolt11 first
        invoice = None
        checking_id = None
        is_arbitrary = False

        if self._is_bolt11(quote.request):
            try:
                invoice = decode(quote.request)
                checking_id = invoice.payment_hash
            except Exception as e:
                logger.warning(f"Failed to decode as bolt11: {e}")
                if not settings.fakewallet_accept_arbitrary_melt_requests:
                    raise
                is_arbitrary = True
        else:
            # Non-bolt11 request
            if not settings.fakewallet_accept_arbitrary_melt_requests:
                raise ValueError(
                    "Request is not a valid bolt11 invoice and "
                    "FAKEWALLET_ACCEPT_ARBITRARY_MELT_REQUESTS is not enabled"
                )
            is_arbitrary = True

        if is_arbitrary:
            checking_id = self._get_checking_id_for_arbitrary(quote.request)
            identifier, amount = self._parse_arbitrary_request(quote.request)

            # Track the arbitrary payment
            self.arbitrary_payments[checking_id] = {
                "request": quote.request,
                "identifier": identifier,
                "amount": amount,
                "status": "pending",
            }
            logger.info(
                f"Created arbitrary payment {checking_id[:16]}... "
                f"for {identifier} amount={amount}"
            )

        # Apply delay if configured
        if settings.fakewallet_delay_outgoing_payment:
            await asyncio.sleep(settings.fakewallet_delay_outgoing_payment)

        # Handle explicit state override
        if settings.fakewallet_pay_invoice_state:
            state = settings.fakewallet_pay_invoice_state
            if state == "SETTLED" and invoice:
                self.update_balance(invoice, incoming=False)
            elif state == "SETTLED" and is_arbitrary:
                # Mark arbitrary payment as settled
                if checking_id in self.arbitrary_payments:
                    self.arbitrary_payments[checking_id]["status"] = "settled"

            return PaymentResponse(
                result=PaymentResult[state],
                checking_id=checking_id,
                fee=Amount(unit=self.unit, amount=settings.fakewallet_arbitrary_melt_fee_sat),
                preimage=self.payment_secrets.get(checking_id) or "0" * 64,
            )

        # Default behavior for bolt11
        if invoice:
            if invoice.payment_hash in self.payment_secrets or settings.fakewallet_brr:
                if invoice not in self.paid_invoices_outgoing:
                    self.paid_invoices_outgoing.append(invoice)
                else:
                    raise ValueError("Invoice already paid")

                self.update_balance(invoice, incoming=False)
                return PaymentResponse(
                    result=PaymentResult.SETTLED,
                    checking_id=invoice.payment_hash,
                    fee=Amount(unit=self.unit, amount=1),
                    preimage=self.payment_secrets.get(invoice.payment_hash) or "0" * 64,
                )
            else:
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message="Only internal invoices can be used!",
                )

        # For arbitrary requests without explicit state, return PENDING
        # Admin must approve via gRPC UpdateNut05Quote
        return PaymentResponse(
            result=PaymentResult.PENDING,
            checking_id=checking_id,
            fee=Amount(unit=self.unit, amount=settings.fakewallet_arbitrary_melt_fee_sat),
            preimage="0" * 64,
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        invoice = next(
            (i for i in self.created_invoices if i.payment_hash == checking_id), None
        ) or self.create_dummy_bolt11(checking_id)

        paid_checking_ids = [i.payment_hash for i in self.paid_invoices_incoming]

        # Check all conditions for "paid":
        # 1. Already in paid_invoices_incoming
        # 2. Auto-pay enabled (FAKEWALLET_BRR)
        # 3. Manually approved via approve_invoice()
        is_paid = (
            checking_id in paid_checking_ids
            or settings.fakewallet_brr
            or checking_id in self.manually_approved_invoices
        )

        if is_paid:
            await self.mark_invoice_paid(invoice, delay=False)
            return PaymentStatus(result=PaymentResult.SETTLED)
        else:
            return PaymentStatus(
                result=PaymentResult.UNKNOWN, error_message="Invoice not paid"
            )

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        if settings.fakewallet_payment_state_exception:
            raise Exception("FakeWallet get_payment_status exception")

        # Check arbitrary payments first
        if checking_id in self.arbitrary_payments:
            payment = self.arbitrary_payments[checking_id]
            status = payment.get("status", "pending")
            if status == "settled":
                return PaymentStatus(result=PaymentResult.SETTLED)
            elif status == "failed":
                return PaymentStatus(result=PaymentResult.FAILED)
            else:
                return PaymentStatus(result=PaymentResult.PENDING)

        # Check bolt11 payments
        if settings.fakewallet_payment_state:
            return PaymentStatus(
                result=PaymentResult[settings.fakewallet_payment_state]
            )

        return PaymentStatus(result=PaymentResult.SETTLED)

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        # Try to decode as bolt11 first
        invoice_obj = None
        checking_id = None
        amount_msat = 0

        if self._is_bolt11(melt_quote.request):
            try:
                invoice_obj = decode(melt_quote.request)
                assert invoice_obj.amount_msat, "invoice has no amount."
                amount_msat = int(invoice_obj.amount_msat)
                checking_id = invoice_obj.payment_hash
            except Exception as e:
                logger.warning(f"Failed to decode as bolt11: {e}")
                if not settings.fakewallet_accept_arbitrary_melt_requests:
                    raise
                # Fall through to arbitrary handling
                invoice_obj = None

        # Handle arbitrary request
        if invoice_obj is None and settings.fakewallet_accept_arbitrary_melt_requests:
            checking_id = self._get_checking_id_for_arbitrary(melt_quote.request)
            identifier, amount = self._parse_arbitrary_request(melt_quote.request)

            amount_msat = amount * 1000

            logger.info(
                f"Created payment quote for arbitrary request: "
                f"{identifier} -> {amount} sats"
            )

        elif invoice_obj is None:
            # Not bolt11 and arbitrary requests not enabled
            raise ValueError(
                "Request is not a valid bolt11 invoice and "
                "FAKEWALLET_ACCEPT_ARBITRARY_MELT_REQUESTS is not enabled"
            )

        # Calculate fees and amounts
        if self.unit == Unit.sat or self.unit == Unit.msat:
            fees_msat = fee_reserve(amount_msat)
            fees = Amount(unit=Unit.msat, amount=fees_msat)
            amount = Amount(unit=Unit.msat, amount=amount_msat)
        elif self.unit == Unit.usd or self.unit == Unit.eur:
            amount_usd = math.ceil(amount_msat / 1e9 * self.fake_btc_price)
            amount = Amount(unit=self.unit, amount=amount_usd)
            fees = Amount(unit=self.unit, amount=2)
        else:
            raise NotImplementedError()

        return PaymentQuoteResponse(
            checking_id=checking_id,
            fee=fees.to(self.unit, round="up"),
            amount=amount.to(self.unit, round="up"),
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        while True:
            value: Bolt11 = await self.paid_invoices_queue.get()
            yield value.payment_hash

    def get_pending_arbitrary_payments(self) -> List[Dict]:
        """Get all arbitrary payments pending approval.

        Returns:
            List of dicts with checking_id, request, identifier, amount, status
        """
        return [
            {
                "checking_id": checking_id,
                "request": payment["request"],
                "identifier": payment["identifier"],
                "amount": payment["amount"],
                "status": payment["status"],
            }
            for checking_id, payment in self.arbitrary_payments.items()
            if payment["status"] == "pending"
        ]

    def get_arbitrary_payment(self, checking_id: str) -> Optional[Dict]:
        """Get details of an arbitrary payment.

        Args:
            checking_id: The checking ID of the payment

        Returns:
            Dict with payment details or None if not found
        """
        return self.arbitrary_payments.get(checking_id)
