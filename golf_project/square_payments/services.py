"""
Square Payment Services
Handles all communication with the Square Payments API (squareup SDK v44+).
"""
import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def get_square_client():
    """Returns an authenticated Square API client (SDK v44+ style)."""
    from square import Square
    from square.environment import SquareEnvironment

    env = (
        SquareEnvironment.SANDBOX
        if settings.SQUARE_ENVIRONMENT == 'sandbox'
        else SquareEnvironment.PRODUCTION
    )
    return Square(
        token=settings.SQUARE_ACCESS_TOKEN,
        environment=env,
    )


def create_payment(source_id: str, amount_cents: int, currency: str, idempotency_key: str, note: str = None, metadata: dict = None):
    """
    Charge a card using a source_id (nonce from the frontend Web SDK).

    Returns:
        The Square Payment object (Pydantic model) on success.
    Raises:
        ValueError: On API error with a human-readable message.
    """
    client = get_square_client()

    # Build keyword arguments for the new SDK style
    kwargs = dict(
        source_id=source_id,
        idempotency_key=idempotency_key,
        amount_money={"amount": amount_cents, "currency": currency},
        location_id=settings.SQUARE_LOCATION_ID,
    )

    # Use reference_id to store temp_id (max 40 chars) for Square dashboard lookup
    if metadata and metadata.get('temp_id'):
        kwargs['reference_id'] = str(metadata['temp_id'])[:40]

    # Build a rich note combining the user note + metadata for audit trail
    note_parts = []
    if note:
        note_parts.append(note)
    if metadata:
        for k, v in metadata.items():
            if k != 'temp_id':
                note_parts.append(f"{k}={v}")
    if note_parts:
        kwargs['note'] = ' | '.join(note_parts)[:500]

    logger.info(
        f"Creating Square payment: idempotency_key={idempotency_key}, "
        f"amount={amount_cents} {currency}, reference_id={kwargs.get('reference_id')}"
    )

    try:
        response = client.payments.create(**kwargs)
        # response is a typed CreatePaymentResponse object; .payment is the Payment model
        payment = response.payment
        logger.info(f"Square payment success: id={payment.id}, status={payment.status}")
        return payment
    except Exception as exc:
        logger.error(f"Square payment error: {exc}")
        raise ValueError(str(exc))


def verify_webhook_signature(request_body: bytes, signature_header: str, signature_key: str, notification_url: str = '') -> bool:
    """
    Verify that an incoming webhook request came from Square.

    Square computes the HMAC-SHA256 signature over:
        notification_url + raw_body   (concatenated as bytes)

    Then base64-encodes the digest and sends it in the
    'x-square-hmacsha256-signature' header.
    """
    import hmac
    import hashlib
    import base64

    if not signature_key or not signature_header:
        logger.warning("Square webhook signature skipped (no key or header configured).")
        return True

    # Square signs: notification_url (str) + body (bytes), all as bytes
    payload = notification_url.encode('utf-8') + request_body

    mac = hmac.new(signature_key.encode('utf-8'), payload, hashlib.sha256)
    expected = base64.b64encode(mac.digest()).decode('utf-8')

    is_valid = hmac.compare_digest(expected, signature_header)
    if not is_valid:
        logger.warning(
            f"Square webhook signature mismatch! "
            f"expected={expected[:20]}... got={signature_header[:20]}..."
        )
    return is_valid
