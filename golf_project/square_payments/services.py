"""
Square Payment Services
=======================

Functions in this file:

  get_square_client(access_token=None)
      Returns an authenticated Square SDK client.
      Pass access_token to use a per-location OAuth token; omit it to fall
      back to the global SQUARE_ACCESS_TOKEN (legacy single-account mode).

  create_payment(...)
      Charge a card using a source_id (nonce from the frontend Web SDK).
      Pass access_token + location_id to route money to the client's account.

  verify_webhook_signature(...)
      Verify that an incoming webhook request came from Square.

  --- OAuth helpers ---

  build_oauth_url(state)
      Build the Square OAuth authorize URL to redirect the golf-center owner.

  exchange_oauth_code(code)
      POST to Square to exchange an auth code for access/refresh tokens.

  refresh_oauth_token(refresh_token)
      POST to Square to get a fresh access token using a refresh token.

  fetch_square_locations(access_token)
      GET /v2/locations using the given access token; returns list of dicts.

  get_location_square_account(ghl_location_id)
      Fetch the LocationSquareAccount for a given GHL location_id.
      Raises ValueError if not found or not connected.
"""
import logging
import requests as req
from django.conf import settings

logger = logging.getLogger(__name__)


def _extract_square_error_message(exc: Exception) -> str:
    """
    Extract a clean, human-readable error message from a Square SDK exception.

    Square exceptions can carry the full HTTP response (headers, body, etc.)
    when converted to str(). This function digs into the response body and
    returns only the 'detail' field from the first error in the errors array,
    falling back to progressively less specific messages if parsing fails.
    """
    try:
        # The Square SDK typically stores the response body on .body or .errors
        body = getattr(exc, 'body', None)
        if isinstance(body, dict):
            errors = body.get('errors', [])
        else:
            # Try parsing from the exception string — look for body: {...}
            import re, ast
            raw = str(exc)
            match = re.search(r"body:\s*(\{.+\})", raw, re.DOTALL)
            if match:
                body_dict = ast.literal_eval(match.group(1))
                errors = body_dict.get('errors', [])
            else:
                errors = []

        if errors:
            detail = errors[0].get('detail', '')
            if detail:
                return detail

    except Exception:
        pass  # Fall through to default

    # Last resort: return a generic message instead of the raw dump
    return 'Payment processing failed. Please try again or contact support.'


# ---------------------------------------------------------------------------
# Square SDK client
# ---------------------------------------------------------------------------

def get_square_client(access_token: str):
    """
    Returns an authenticated Square API client (SDK v44+ style).

    Args:
        access_token: The per-location OAuth access token (required).
                      Every location must connect their own Square account.
                      There is no fallback to a global/platform token.
    """
    if not access_token or not access_token.strip():
        raise ValueError(
            "No Square access token provided. "
            "The golf center must connect their Square account first."
        )

    from square import Square
    from square.environment import SquareEnvironment

    env = (
        SquareEnvironment.SANDBOX
        if settings.SQUARE_ENVIRONMENT == 'sandbox'
        else SquareEnvironment.PRODUCTION
    )

    return Square(token=access_token.strip(), environment=env)


# ---------------------------------------------------------------------------
# Payment creation
# ---------------------------------------------------------------------------

def create_payment(
    source_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    note: str = None,
    metadata: dict = None,
    access_token: str = None,
    location_id: str = None,
):
    """
    Charge a card using a source_id (nonce from the frontend Web SDK).

    Args:
        source_id:        Card nonce from Square Web SDK.
        amount_cents:     Amount to charge in smallest currency unit.
        currency:         ISO 4217 currency code (e.g. 'CAD', 'USD').
        idempotency_key:  Unique key to prevent duplicate charges.
        note:             Optional human-readable note for the payment.
        metadata:         Optional dict stored on the payment (temp_id etc.).
        access_token:     Per-location OAuth token (required — no global fallback).
        location_id:      Square Location ID (required — no global fallback).

    Returns:
        The Square Payment object (Pydantic model) on success.

    Raises:
        ValueError: On API error with a human-readable message.
    """
    client = get_square_client(access_token)

    # location_id is required — no fallback to global SQUARE_LOCATION_ID
    sq_location_id = (location_id or '').strip()
    if not sq_location_id:
        raise ValueError(
            "square_location_id is required to create a payment. "
            "The golf center must connect their Square account."
        )

    kwargs = dict(
        source_id=source_id,
        idempotency_key=idempotency_key,
        amount_money={"amount": amount_cents, "currency": currency},
        location_id=sq_location_id,
    )

    # Store temp_id as reference_id (max 40 chars) for Square dashboard lookup
    if metadata and metadata.get('temp_id'):
        kwargs['reference_id'] = str(metadata['temp_id'])[:40]

    # Build a rich note for the audit trail
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
        "Creating Square payment: idempotency_key=%s, amount=%d %s, "
        "location_id=%s, reference_id=%s",
        idempotency_key, amount_cents, currency,
        sq_location_id, kwargs.get('reference_id'),
    )

    try:
        response = client.payments.create(**kwargs)
        payment = response.payment
        logger.info("Square payment success: id=%s, status=%s", payment.id, payment.status)
        return payment
    except Exception as exc:
        # Extract clean human-readable message from Square's error response
        human_msg = _extract_square_error_message(exc)
        logger.error("Square payment error (%s): %s", type(exc).__name__, human_msg)
        raise ValueError(human_msg)



# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(
    request_body: bytes,
    signature_header: str,
    signature_key: str,
    notification_url: str = '',
) -> bool:
    """
    Verify that an incoming webhook request came from Square.

    Square computes HMAC-SHA256 over: notification_url + raw_body
    then base64-encodes the digest.
    """
    import hmac
    import hashlib
    import base64

    if not signature_key or not signature_header:
        logger.warning("Square webhook signature skipped (no key or header configured).")
        return True

    payload = notification_url.encode('utf-8') + request_body
    mac = hmac.new(signature_key.encode('utf-8'), payload, hashlib.sha256)
    expected = base64.b64encode(mac.digest()).decode('utf-8')

    is_valid = hmac.compare_digest(expected, signature_header)
    if not is_valid:
        logger.warning(
            "Square webhook signature mismatch! expected=%s... got=%s...",
            expected[:20], signature_header[:20],
        )
    return is_valid


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _square_oauth_base_url() -> str:
    """Return the correct Square base URL depending on environment."""
    if settings.SQUARE_ENVIRONMENT == 'sandbox':
        return 'https://connect.squareupsandbox.com'
    return 'https://connect.squareup.com'


def build_oauth_url(state: str = '') -> str:
    """
    Build the Square OAuth authorize URL (Code flow, not PKCE).

    Square docs: for the code flow, redirect_uri must be registered in the
    Developer Dashboard. Do NOT include it in the authorization URL — Square
    uses the registered one automatically. Including it causes a 400 if there
    is any mismatch (even a trailing slash difference).

    Args:
        state: CSRF token / opaque value (e.g. ghl_location_id).

    Returns:
        Full authorization URL string.
    """
    from urllib.parse import urlencode

    params = {
        'client_id': settings.SQUARE_APPLICATION_ID,
        'scope': 'PAYMENTS_WRITE PAYMENTS_READ MERCHANT_PROFILE_READ ITEMS_WRITE ITEMS_READ SUBSCRIPTIONS_WRITE SUBSCRIPTIONS_READ CUSTOMERS_WRITE CUSTOMERS_READ',
        'session': 'false',
    }
    if state:
        params['state'] = state

    base = _square_oauth_base_url()
    return f"{base}/oauth2/authorize?{urlencode(params)}"




def exchange_oauth_code(code: str) -> dict:
    """
    Exchange an authorization code for access/refresh tokens.

    POST https://connect.squareup.com/oauth2/token

    Returns:
        dict with keys: access_token, refresh_token, merchant_id,
                        expires_at (ISO 8601 string), token_type.

    Raises:
        ValueError: On HTTP or API error.
    """
    app_id = settings.SQUARE_APPLICATION_ID
    app_secret = settings.SQUARE_OAUTH_SECRET
    redirect_uri = settings.SQUARE_OAUTH_REDIRECT_URI
    base = _square_oauth_base_url()

    payload = {
        'client_id': app_id,
        'client_secret': app_secret,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': redirect_uri,
    }

    try:
        resp = req.post(f"{base}/oauth2/token", json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except req.exceptions.HTTPError as exc:
        error_text = exc.response.text if exc.response else str(exc)
        logger.error("Square OAuth token exchange failed: %s — %s", exc, error_text)
        raise ValueError(f"Square OAuth exchange failed: {error_text}")
    except Exception as exc:
        logger.error("Square OAuth token exchange error: %s", exc)
        raise ValueError(str(exc))

    if 'access_token' not in data:
        logger.error("Square OAuth: no access_token in response: %s", data)
        raise ValueError(data.get('message', 'Square did not return an access token.'))

    return data


def refresh_oauth_token(refresh_token: str) -> dict:
    """
    Obtain a fresh access token using a refresh token.

    POST https://connect.squareup.com/oauth2/token  (grant_type=refresh_token)

    Returns:
        dict with keys: access_token, expires_at, token_type, merchant_id.

    Raises:
        ValueError: On HTTP or API error.
    """
    app_id = settings.SQUARE_APPLICATION_ID
    app_secret = settings.SQUARE_OAUTH_SECRET
    base = _square_oauth_base_url()

    payload = {
        'client_id': app_id,
        'client_secret': app_secret,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
    }

    try:
        resp = req.post(f"{base}/oauth2/token", json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except req.exceptions.HTTPError as exc:
        error_text = exc.response.text if exc.response else str(exc)
        logger.error("Square token refresh failed: %s — %s", exc, error_text)
        raise ValueError(f"Square token refresh failed: {error_text}")
    except Exception as exc:
        logger.error("Square token refresh error: %s", exc)
        raise ValueError(str(exc))

    if 'access_token' not in data:
        logger.error("Square token refresh: no access_token in response: %s", data)
        raise ValueError(data.get('message', 'Square did not return an access token on refresh.'))

    return data


def fetch_square_locations(access_token: str) -> list:
    """
    GET /v2/locations using the given access token.

    Returns a list of Square location dicts.
    Each dict has at least: id, name, status.
    """
    client = get_square_client(access_token)
    try:
        resp = client.locations.list()
        locs = resp.locations or []
        return [
            {
                'id': loc.id,
                'name': getattr(loc, 'name', ''),
                'status': getattr(loc, 'status', ''),
            }
            for loc in locs
        ]
    except Exception as exc:
        logger.error("fetch_square_locations error: %s", exc)
        return []


def get_location_square_account(ghl_location_id: str):
    """
    Fetch the LocationSquareAccount for a given GHL location_id.

    Args:
        ghl_location_id: The GHLLocation.location_id string.

    Returns:
        LocationSquareAccount instance.

    Raises:
        ValueError: If the location has not connected Square.
    """
    from .models import LocationSquareAccount

    try:
        account = LocationSquareAccount.objects.select_related('ghl_location').get(
            ghl_location__location_id=ghl_location_id,
            is_connected=True,
        )
    except LocationSquareAccount.DoesNotExist:
        raise ValueError(
            f"Location '{ghl_location_id}' has not connected a Square account. "
            "Please complete Square OAuth first."
        )
    return account


# ---------------------------------------------------------------------------
# Subscription helpers (Membership feature)
# ---------------------------------------------------------------------------

def get_or_create_square_customer(access_token: str, user) -> str:
    """
    Upsert a Square Customer for the given platform user.
    Searches by phone first; creates a new customer if not found.
    Returns the Square customer_id string.
    """
    import uuid as _uuid
    client = get_square_client(access_token)

    # Search by phone number first
    if user.phone:
        try:
            result = client.customers.search(
                query={
                    'filter': {
                        'phone_filter': {'phone': user.phone}
                    }
                }
            )
            customers = getattr(result, 'customers', None) or []
            if customers:
                return customers[0].id
        except Exception:
            pass  # Fall through to create

    # Create a new customer
    body = {
        'idempotency_key': str(_uuid.uuid4()),
        'given_name': getattr(user, 'first_name', '') or '',
        'family_name': getattr(user, 'last_name', '') or '',
        'email_address': getattr(user, 'email', '') or '',
        'phone_number': getattr(user, 'phone', '') or '',
    }
    try:
        result = client.customers.create(**body)
        return result.customer.id
    except Exception as exc:
        human_msg = _extract_square_error_message(exc)
        logger.error('get_or_create_square_customer error: %s', human_msg)
        raise ValueError(human_msg)


def save_card_on_file(access_token: str, customer_id: str, source_id: str, idempotency_key: str = None) -> str:
    """
    Save a card nonce to a Square customer as a Card on File.
    Returns the Square card_id string.
    """
    import uuid as _uuid
    client = get_square_client(access_token)
    body = {
        'idempotency_key': idempotency_key or str(_uuid.uuid4()),
        'source_id': source_id,
        'card': {'customer_id': customer_id},
    }
    try:
        result = client.cards.create_card(**body)
        return result.card.id
    except Exception as exc:
        human_msg = _extract_square_error_message(exc)
        logger.error('save_card_on_file error: %s', human_msg)
        raise ValueError(human_msg)


def upsert_subscription_plan(access_token: str, location_id: str, package) -> tuple:
    """
    Upsert a Square Catalog subscription plan for the given SimulatorPackage.
    Uses the package ID as an idempotency key — safe to call repeatedly.
    Returns (catalog_item_id, plan_variation_id).
    """
    client = get_square_client(access_token)

    plan_name = f'{package.title} — Monthly Membership'
    # Calculate tax-inclusive price (HST 14%)
    HST_RATE = 0.14
    base_price = float(package.price)
    tax_amount = round(base_price * HST_RATE, 2)
    tax_inclusive_price = round(base_price + tax_amount, 2)
    price_cents = int(round(tax_inclusive_price * 100))
    item_id = f'#plan-pkg-{package.id}'
    variation_id = f'#variation-pkg-{package.id}'
    idem_key = f'sub-plan-pkg-{package.id}'

    body = {
        'idempotency_key': idem_key,
        'object': {
            'type': 'SUBSCRIPTION_PLAN',
            'id': item_id,
            'subscription_plan_data': {
                'name': plan_name,
                'subscription_plan_variations': [
                    {
                        'type': 'SUBSCRIPTION_PLAN_VARIATION',
                        'id': variation_id,
                        'subscription_plan_variation_data': {
                            'name': 'Monthly',
                            'phases': [
                                {
                                    'cadence': 'MONTHLY',
                                    'recurring': True,
                                    'pricing': {
                                        'type': 'STATIC',
                                        'price_money': {
                                            'amount': price_cents,
                                            'currency': 'CAD',
                                        },
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        },
    }

    try:
        result = client.catalog.upsert_catalog_object(**body)
        obj = result.catalog_object
        catalog_item_id = obj.id
        variations = obj.subscription_plan_data.subscription_plan_variations
        plan_variation_id = variations[0].id
        logger.info(
            'Upserted subscription plan for package %s: catalog=%s variation=%s',
            package.id, catalog_item_id, plan_variation_id,
        )
        return catalog_item_id, plan_variation_id
    except Exception as exc:
        human_msg = _extract_square_error_message(exc)
        logger.error('upsert_subscription_plan error for package %s: %s', package.id, human_msg)
        raise ValueError(human_msg)


def create_square_subscription(
    access_token: str,
    location_id: str,
    plan_variation_id: str,
    customer_id: str,
    card_id: str = None,
    idempotency_key: str = None,
) -> dict:
    """
    Create a Square recurring subscription.
    Returns a dict with subscription details.
    """
    import uuid as _uuid
    client = get_square_client(access_token)

    body = {
        'idempotency_key': idempotency_key or str(_uuid.uuid4()),
        'location_id': location_id,
        'plan_variation_id': plan_variation_id,
        'customer_id': customer_id,
    }
    if card_id:
        body['card_id'] = card_id

    try:
        result = client.subscriptions.create_subscription(**body)
        sub = result.subscription
        return {
            'id': sub.id,
            'status': getattr(sub, 'status', 'ACTIVE').lower(),
            'start_date': getattr(sub, 'start_date', None),
            'charged_through_date': getattr(sub, 'charged_through_date', None),
            'customer_id': getattr(sub, 'customer_id', customer_id),
            'plan_variation_id': getattr(sub, 'plan_variation_id', plan_variation_id),
        }
    except Exception as exc:
        human_msg = _extract_square_error_message(exc)
        logger.error('create_square_subscription error: %s', human_msg)
        raise ValueError(human_msg)


def cancel_square_subscription(access_token: str, subscription_id: str) -> dict:
    """
    Cancel a Square subscription.
    Returns a dict with updated subscription status.
    """
    client = get_square_client(access_token)
    try:
        result = client.subscriptions.cancel_subscription(subscription_id=subscription_id)
        sub = result.subscription
        return {
            'id': sub.id,
            'status': getattr(sub, 'status', 'CANCELED').lower(),
        }
    except Exception as exc:
        human_msg = _extract_square_error_message(exc)
        logger.error('cancel_square_subscription error: %s', human_msg)
        raise ValueError(human_msg)
