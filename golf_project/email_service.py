"""
Email Invoice Service
=====================
Sends professional HTML invoice emails to customers after successful payments.

Usage
-----
    from email_service import send_invoice_email

    send_invoice_email(
        customer_email='john@example.com',
        customer_name='John Doe',
        payment_id='sq_pay_ABC123',
        payment_type='simulator',        # simulator | package | event | asset | subscription
        item_description='Simulator Booking — Bay 1 & 2',
        base_amount=90.00,
        discount_amount=10.00,           # 0 if no coupon
        coupon_code='SAVE10',            # '' if no coupon
        tax_rate=0.14,
        tax_amount=11.20,
        total_amount=91.20,
        ghl_location=<GHLLocation instance>,  # for logo / contact details
        booking_date=datetime.now(),     # optional, defaults to now
    )

The function returns True on success and False on failure.
It NEVER raises — invoice failure must not break payment flow.
"""

import logging
from datetime import datetime
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _build_invoice_html(
    *,
    customer_name: str,
    customer_email: str,
    payment_id: str,
    payment_type: str,
    item_description: str,
    base_amount: float,
    discount_amount: float,
    coupon_code: str,
    tax_rate: float,
    tax_amount: float,
    total_amount: float,
    company_name: str,
    logo_url: str,
    contact_phone: str,
    support_email: str,
    business_id: str,
    refund_policy: str,
    invoice_date: str,
) -> str:
    """Return a complete HTML string for the invoice email."""

    # ── Friendly payment type label ────────────────────────────────────────
    type_labels = {
        'simulator': 'Simulator Booking',
        'package': 'Package Purchase',
        'event': 'Event Registration',
        'asset': 'Asset Booking',
        'subscription': 'Monthly Membership Renewal',
    }
    payment_label = type_labels.get(payment_type, payment_type.replace('_', ' ').title())

    # ── Tax percentage for display (e.g. 0.14 → "14%") ────────────────────
    tax_pct = f"{int(round(tax_rate * 100))}%"

    # ── Short invoice reference number ─────────────────────────────────────
    invoice_ref = payment_id[-12:].upper() if payment_id else 'N/A'

    # ── Logo block ─────────────────────────────────────────────────────────
    if logo_url:
        logo_block = f'''
            <div style="text-align:center; margin-bottom:8px;">
                <img src="{logo_url}" alt="{company_name}" 
                     style="max-height:70px; max-width:260px; object-fit:contain;" />
            </div>'''
    else:
        logo_block = ''

    # ── Discount row (only shown when a coupon was applied) ────────────────
    if discount_amount and discount_amount > 0 and coupon_code:
        discount_row = f'''
            <tr>
                <td style="padding:10px 0; color:#16a34a; font-size:14px;">
                    Coupon Discount
                    <span style="font-size:12px; background:#dcfce7; color:#16a34a; 
                                 padding:2px 7px; border-radius:9999px; margin-left:6px;">
                        {coupon_code}
                    </span>
                </td>
                <td style="padding:10px 0; text-align:right; color:#16a34a; font-size:14px;">
                    &minus; ${discount_amount:.2f}
                </td>
            </tr>'''
    else:
        discount_row = ''

    # ── Contact details footer items ───────────────────────────────────────
    contact_parts = []
    if contact_phone:
        contact_parts.append(
            f'<span>📞 {contact_phone}</span>'
        )
    if support_email:
        contact_parts.append(
            f'<span>✉ <a href="mailto:{support_email}" style="color:#22c55e; text-decoration:none;">{support_email}</a></span>'
        )
    if business_id:
        contact_parts.append(
            f'<span>🏢 Business ID: {business_id}</span>'
        )
    contact_html = '&nbsp;&nbsp;|&nbsp;&nbsp;'.join(contact_parts) if contact_parts else ''

    # ── Refund policy block (only shown when set) ─────────────────────────
    if refund_policy:
        # Replace newlines with <br> for HTML rendering
        refund_policy_html_text = refund_policy.replace('\n', '<br/>')
        refund_policy_block = f'''
          <!-- ── Refund Policy ─────────────────────────────────────────── -->
          <tr>
            <td style="padding:0 40px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="border:1px solid #e2e8f0; border-radius:10px; overflow:hidden;">
                <tr style="background:#f8fafc;">
                  <td style="padding:10px 16px; border-bottom:1px solid #e2e8f0;">
                    <span style="font-size:12px; font-weight:700; color:#64748b;
                                 text-transform:uppercase; letter-spacing:.6px;">Refund &amp; Cancellation Policy</span>
                  </td>
                </tr>
                <tr>
                  <td style="padding:14px 16px; font-size:13px; color:#475569; line-height:1.7;">
                    {refund_policy_html_text}
                  </td>
                </tr>
              </table>
            </td>
          </tr>'''
    else:
        refund_policy_block = ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Invoice #{invoice_ref}</title>
</head>
<body style="margin:0; padding:0; background:#f1f5f9; font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">

  <!-- wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background:#f1f5f9; padding:40px 0;">
    <tr>
      <td align="center">

        <!-- card -->
        <table width="620" cellpadding="0" cellspacing="0" role="presentation"
               style="background:#ffffff; border-radius:16px; overflow:hidden;
                      box-shadow:0 4px 24px rgba(0,0,0,.08); max-width:620px; width:100%;">

          <!-- ── Header ────────────────────────────────────────────────── -->
          <tr>
            <td style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);
                        padding:36px 40px 28px; text-align:center;">
              {logo_block}
              <h1 style="margin:0; color:#ffffff; font-size:22px; font-weight:700;
                          letter-spacing:.5px;">{company_name}</h1>
              <p style="margin:6px 0 0; color:#94a3b8; font-size:13px;">Payment Receipt</p>
            </td>
          </tr>

          <!-- ── Invoice meta strip ────────────────────────────────────── -->
          <tr>
            <td style="background:#0f172a; padding:12px 40px;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
                <tr>
                  <td style="color:#94a3b8; font-size:12px;">
                    Invoice&nbsp;<strong style="color:#e2e8f0;">#{invoice_ref}</strong>
                  </td>
                  <td style="color:#94a3b8; font-size:12px; text-align:right;">
                    Date:&nbsp;<strong style="color:#e2e8f0;">{invoice_date}</strong>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- ── Body ──────────────────────────────────────────────────── -->
          <tr>
            <td style="padding:32px 40px 20px;">

              <!-- Greeting -->
              <p style="margin:0 0 20px; color:#0f172a; font-size:16px; font-weight:600;">
                Hi {customer_name},
              </p>
              <p style="margin:0 0 28px; color:#475569; font-size:14px; line-height:1.6;">
                Thank you for your payment! Here is your receipt for the
                <strong>{payment_label}</strong> at <strong>{company_name}</strong>.
              </p>

              <!-- Item table -->
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="border-radius:10px; overflow:hidden; border:1px solid #e2e8f0;">

                <!-- table header -->
                <tr style="background:#f8fafc;">
                  <th style="padding:12px 16px; text-align:left; font-size:12px;
                              font-weight:600; color:#64748b; text-transform:uppercase;
                              letter-spacing:.6px; border-bottom:1px solid #e2e8f0;">
                    Description
                  </th>
                  <th style="padding:12px 16px; text-align:right; font-size:12px;
                              font-weight:600; color:#64748b; text-transform:uppercase;
                              letter-spacing:.6px; border-bottom:1px solid #e2e8f0;">
                    Amount
                  </th>
                </tr>

                <!-- item row -->
                <tr>
                  <td style="padding:14px 16px; color:#0f172a; font-size:14px;
                              border-bottom:1px solid #f1f5f9;">
                    {item_description}
                  </td>
                  <td style="padding:14px 16px; text-align:right; color:#0f172a;
                              font-size:14px; font-weight:600; border-bottom:1px solid #f1f5f9;">
                    ${base_amount:.2f}
                  </td>
                </tr>
              </table>

              <!-- Totals block -->
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="margin-top:4px; border:1px solid #e2e8f0; border-top:none;
                            border-radius:0 0 10px 10px; overflow:hidden;">
                <tr style="background:#f8fafc;">
                  <td style="padding:0 16px;">
                    <table width="100%" cellpadding="0" cellspacing="0" role="presentation">

                      <!-- Subtotal -->
                      <tr>
                        <td style="padding:10px 0; color:#64748b; font-size:14px;
                                    border-bottom:1px solid #e2e8f0;">
                          Subtotal
                        </td>
                        <td style="padding:10px 0; text-align:right; color:#64748b;
                                    font-size:14px; border-bottom:1px solid #e2e8f0;">
                          ${base_amount:.2f}
                        </td>
                      </tr>

                      {discount_row}

                      <!-- HST -->
                      <tr>
                        <td style="padding:10px 0; color:#64748b; font-size:14px;
                                    border-bottom:1px solid #e2e8f0;">
                          HST ({tax_pct})
                        </td>
                        <td style="padding:10px 0; text-align:right; color:#64748b;
                                    font-size:14px; border-bottom:1px solid #e2e8f0;">
                          ${tax_amount:.2f}
                        </td>
                      </tr>

                      <!-- Grand Total -->
                      <tr style="background:#0f172a;">
                        <td style="padding:14px 0; color:#ffffff; font-size:16px;
                                    font-weight:700;">
                          Total Charged
                        </td>
                        <td style="padding:14px 0; text-align:right; color:#22c55e;
                                    font-size:20px; font-weight:800;">
                          ${total_amount:.2f}&nbsp;CAD
                        </td>
                      </tr>

                    </table>
                  </td>
                </tr>
              </table>

              <!-- Payment reference -->
              <p style="margin:20px 0 0; font-size:12px; color:#94a3b8;">
                Payment reference:&nbsp;
                <span style="font-family:monospace; color:#64748b;">{payment_id}</span>
              </p>

            </td>
          </tr>

          <!-- ── Footer ─────────────────────────────────────────────────── -->
          {refund_policy_block}
          <tr>
            <td style="background:#f8fafc; padding:24px 40px;
                        border-top:1px solid #e2e8f0; text-align:center;">
              <p style="margin:0 0 8px; font-size:13px; color:#64748b; line-height:1.8;">
                {contact_html}
              </p>
              <p style="margin:0; font-size:12px; color:#94a3b8;">
                For any questions, please contact us using the details above.<br/>
                <strong style="color:#64748b;">Please do not reply to this email — this mailbox is not monitored.</strong>
              </p>
            </td>
          </tr>

        </table>
        <!-- /card -->

        <p style="margin:20px 0 0; font-size:11px; color:#94a3b8; text-align:center;">
          This is an automated receipt from {company_name}. Please do not reply to this email — this mailbox is not monitored.
        </p>

      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_invoice_email(
    *,
    customer_email: str,
    customer_name: str,
    payment_id: str,
    payment_type: str,
    item_description: str,
    base_amount: float,
    discount_amount: float = 0.0,
    coupon_code: str = '',
    tax_rate: float = 0.14,
    tax_amount: float = 0.0,
    total_amount: float,
    ghl_location=None,
    booking_date=None,
) -> bool:
    """
    Send a payment invoice email to the customer.

    Parameters
    ----------
    customer_email   : Recipient email address.
    customer_name    : Recipient display name.
    payment_id       : Square payment ID (used as invoice reference).
    payment_type     : 'simulator' | 'package' | 'event' | 'asset' | 'subscription'
    item_description : Human-readable description of the purchased item.
    base_amount      : Pre-discount, pre-tax price.
    discount_amount  : Amount discounted by coupon (0 if none).
    coupon_code      : Coupon code string ('' if none).
    tax_rate         : Decimal tax rate, e.g. 0.14 for 14%.
    tax_amount       : Calculated tax (base_amount - discount) * tax_rate.
    total_amount     : Final amount actually charged (post-coupon, post-tax).
    ghl_location     : GHLLocation model instance (provides logo + contact details).
    booking_date     : datetime for the invoice date (defaults to now).

    Returns True on success, False on any error (never raises).
    """
    if not customer_email:
        logger.warning(
            "send_invoice_email: skipped for payment %s — customer has no email address.",
            payment_id,
        )
        return False

    api_key = getattr(settings, 'RESEND_API_KEY', '')
    from_email = getattr(settings, 'RESEND_FROM_EMAIL', 'noreply@performgolf.net')

    if not api_key:
        logger.error("send_invoice_email: RESEND_API_KEY is not configured.")
        return False

    # ── Gather location branding ──────────────────────────────────────────
    company_name = 'PerformGolf'
    logo_url = ''
    contact_phone = ''
    support_email = ''
    business_id = ''
    refund_policy = ''

    if ghl_location:
        company_name = ghl_location.company_name or company_name
        contact_phone = ghl_location.contact_phone or ''
        support_email = ghl_location.support_email or ''
        business_id = ghl_location.business_id or ''
        refund_policy = ghl_location.refund_policy or ''

        # Build absolute logo URL — stored as a relative media path
        if ghl_location.logo:
            try:
                logo_url = ghl_location.logo.url
                # Make it absolute if it's relative (needed for email clients)
                if logo_url and not logo_url.startswith('http'):
                    backend_base = getattr(settings, 'BACKEND_BASE_URL', '').rstrip('/')
                    if backend_base:
                        logo_url = f"{backend_base}{logo_url}"
                    else:
                        logo_url = ''  # can't embed relative URLs in email
            except Exception:
                logo_url = ''

    # ── Format invoice date ───────────────────────────────────────────────
    invoice_dt = booking_date or timezone.now()
    invoice_date = invoice_dt.strftime('%B %d, %Y') if hasattr(invoice_dt, 'strftime') else str(invoice_dt)

    # ── Build HTML ────────────────────────────────────────────────────────
    html_body = _build_invoice_html(
        customer_name=customer_name or customer_email,
        customer_email=customer_email,
        payment_id=payment_id,
        payment_type=payment_type,
        item_description=item_description,
        base_amount=base_amount,
        discount_amount=discount_amount,
        coupon_code=coupon_code,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total_amount=total_amount,
        company_name=company_name,
        logo_url=logo_url,
        contact_phone=contact_phone,
        support_email=support_email,
        business_id=business_id,
        refund_policy=refund_policy,
        invoice_date=invoice_date,
    )

    # ── Subject line ──────────────────────────────────────────────────────
    type_labels = {
        'simulator': 'Simulator Booking',
        'package': 'Package Purchase',
        'event': 'Event Registration',
        'asset': 'Asset Booking',
        'subscription': 'Monthly Membership Renewal',
    }
    type_label = type_labels.get(payment_type, payment_type.title())
    subject = f"Your Invoice — {type_label} at {company_name}"

    # ── Send via Resend ───────────────────────────────────────────────────
    try:
        import resend
        resend.api_key = api_key

        params = {
            "from": f"{company_name} <{from_email}>",
            "to": [customer_email],
            "subject": subject,
            "html": html_body,
        }

        response = resend.Emails.send(params)
        logger.info(
            "Invoice email sent: payment=%s, to=%s, resend_id=%s",
            payment_id, customer_email, response.get('id') if isinstance(response, dict) else getattr(response, 'id', 'N/A'),
        )
        return True

    except ImportError:
        logger.error("send_invoice_email: 'resend' package is not installed. Run: pip install resend")
        return False
    except Exception as exc:
        logger.error(
            "send_invoice_email: failed to send invoice for payment %s to %s: %s",
            payment_id, customer_email, exc, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Convenience helper — resolves customer info from a User instance
# ---------------------------------------------------------------------------

def send_invoice_for_user(
    *,
    user,                   # Django User model instance
    payment_id: str,
    payment_type: str,
    item_description: str,
    base_amount: float,
    discount_amount: float = 0.0,
    coupon_code: str = '',
    tax_rate: float = 0.14,
    tax_amount: float = 0.0,
    total_amount: float,
    ghl_location=None,
    booking_date=None,
) -> bool:
    """
    Shortcut that pulls name + email from a User instance and delegates to
    send_invoice_email(). Returns True/False without raising.
    """
    email = getattr(user, 'email', '') or ''
    first = getattr(user, 'first_name', '') or ''
    last = getattr(user, 'last_name', '') or ''
    full_name = f"{first} {last}".strip() or getattr(user, 'username', '') or email

    return send_invoice_email(
        customer_email=email,
        customer_name=full_name,
        payment_id=payment_id,
        payment_type=payment_type,
        item_description=item_description,
        base_amount=base_amount,
        discount_amount=discount_amount,
        coupon_code=coupon_code,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total_amount=total_amount,
        ghl_location=ghl_location,
        booking_date=booking_date,
    )
