from django.urls import path

from .views import (
    GHLOnboardView, 
    GHLOAuthAuthorizeView, 
    GHLOAuthCallbackView,
    list_onboarded_locations,
    test_contact_custom_fields,
    test_all_custom_fields,
    test_otp_custom_field,
    test_purchase_custom_field,
    list_all_ghl_locations,
    update_ghl_location_company_name,
    set_ghl_location_company_name
)

urlpatterns = [
    path('onboard/', GHLOnboardView.as_view(), name='ghl-onboard'),
    path('oauth/authorize/', GHLOAuthAuthorizeView.as_view(), name='ghl-oauth-authorize'),
    path('oauth/callback/', GHLOAuthCallbackView.as_view(), name='ghl-oauth-callback'),
    path('locations/', list_onboarded_locations, name='ghl-locations-list'),
    path('test-custom-fields/', test_contact_custom_fields, name='ghl-test-custom-fields'),
    path('test-all-fields/', test_all_custom_fields, name='ghl-test-all-fields'),
    path('test-otp-field/', test_otp_custom_field, name='ghl-test-otp-field'),
    path('test-purchase-field/', test_purchase_custom_field, name='ghl-test-purchase-field'),
    # Superadmin endpoints for managing locations
    path('admin/locations/', list_all_ghl_locations, name='ghl-admin-locations-list'),
    path('admin/locations/<str:location_id>/company-name/', update_ghl_location_company_name, name='ghl-update-company-name'),
    path('admin/locations/set-company-name/', set_ghl_location_company_name, name='ghl-set-company-name'),
]

