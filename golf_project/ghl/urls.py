from django.urls import path

from .views import (
    GHLOnboardView, 
    GHLOAuthAuthorizeView, 
    GHLOAuthCallbackView,
    list_onboarded_locations,
)

urlpatterns = [
    path('onboard/', GHLOnboardView.as_view(), name='ghl-onboard'),
    path('oauth/authorize/', GHLOAuthAuthorizeView.as_view(), name='ghl-oauth-authorize'),
    path('oauth/callback/', GHLOAuthCallbackView.as_view(), name='ghl-oauth-callback'),
    path('locations/', list_onboarded_locations, name='ghl-locations-list'),
]

