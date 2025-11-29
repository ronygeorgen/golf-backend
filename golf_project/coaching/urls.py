from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    CoachingPackageViewSet, 
    CoachingPackagePurchaseViewSet,
    GiftClaimView,
    SessionTransferViewSet,
    UserPhoneCheckView,
    PackagePurchaseWebhookView,
    CreateTempPurchaseView,
    ListTempPurchasesView,
    ListPendingRecipientsView
)

router = DefaultRouter()
router.register(r'packages', CoachingPackageViewSet, basename='coaching-package')
router.register(r'purchases', CoachingPackagePurchaseViewSet, basename='coaching-purchase')
router.register(r'transfers', SessionTransferViewSet, basename='session-transfer')

urlpatterns = [
    path('', include(router.urls)),
    path('gifts/claim/<str:token>/', GiftClaimView.as_view(), name='gift-claim'),
    path('users/check-phone/', UserPhoneCheckView.as_view(), name='check-phone'),
    path('temp-purchase/', CreateTempPurchaseView.as_view(), name='create-temp-purchase'),
    path('temp-purchases/', ListTempPurchasesView.as_view(), name='list-temp-purchases'),
    path('pending-recipients/', ListPendingRecipientsView.as_view(), name='list-pending-recipients'),
    path('webhook/purchase/', PackagePurchaseWebhookView.as_view(), name='package-purchase-webhook'),
]



