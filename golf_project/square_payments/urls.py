from django.urls import path
from .views import (
    InitiateSquarePaymentView,
    SquareWebhookView,
    SquareConfigView,
    SquareOAuthAuthorizeView,
    SquareOAuthCallbackView,
    SquareOAuthStatusView,
    SquareOAuthDisconnectView,
    SquareOAuthListView,
)

urlpatterns = [
    # ── Existing payment endpoints ──────────────────────────────────────────
    path('initiate-payment/', InitiateSquarePaymentView.as_view(), name='square-initiate-payment'),
    path('webhook/', SquareWebhookView.as_view(), name='square-webhook'),
    path('config/', SquareConfigView.as_view(), name='square-config'),

    # ── OAuth (per-location multi-tenant) ───────────────────────────────────
    # Step 1: golf-center admin clicks "Connect Square" → redirect to Square login
    path('oauth/authorize/', SquareOAuthAuthorizeView.as_view(), name='square-oauth-authorize'),
    # Step 2: Square redirects back here with ?code=...&state=GHL_LOCATION_ID
    path('oauth/callback/', SquareOAuthCallbackView.as_view(), name='square-oauth-callback'),
    # Status check for a specific location
    path('oauth/status/<str:ghl_location_id>/', SquareOAuthStatusView.as_view(), name='square-oauth-status'),
    # Disconnect Square for a location (admin only)
    path('oauth/disconnect/<str:ghl_location_id>/', SquareOAuthDisconnectView.as_view(), name='square-oauth-disconnect'),
    # List all connected locations (superadmin only)
    path('oauth/list/', SquareOAuthListView.as_view(), name='square-oauth-list'),
]
