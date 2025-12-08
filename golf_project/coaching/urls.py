from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    CoachingPackageViewSet, 
    CoachingPackagePurchaseViewSet,
    GiftClaimView,
    SessionTransferViewSet,
    SimulatorHoursTransferViewSet,
    UserPhoneCheckView,
    PackagePurchaseWebhookView,
    CreateTempPurchaseView,
    ListTempPurchasesView,
    ListPendingRecipientsView,
    SimulatorPackageViewSet,
    SimulatorPackagePurchaseViewSet,
)

router = DefaultRouter()
router.register(r'packages', CoachingPackageViewSet, basename='coaching-package')
router.register(r'purchases', CoachingPackagePurchaseViewSet, basename='coaching-purchase')
router.register(r'transfers', SessionTransferViewSet, basename='session-transfer')
router.register(r'simulator-packages', SimulatorPackageViewSet, basename='simulator-package')
router.register(r'simulator-purchases', SimulatorPackagePurchaseViewSet, basename='simulator-purchase')
router.register(r'simulator-transfers', SimulatorHoursTransferViewSet, basename='simulator-hours-transfer')

urlpatterns = [
    path('', include(router.urls)),
    path('gifts/claim/<str:token>/', GiftClaimView.as_view(), name='gift-claim'),
    path('users/check-phone/', UserPhoneCheckView.as_view(), name='check-phone'),
    path('temp-purchase/', CreateTempPurchaseView.as_view(), name='create-temp-purchase'),
    path('temp-purchases/', ListTempPurchasesView.as_view(), name='list-temp-purchases'),
    path('pending-recipients/', ListPendingRecipientsView.as_view(), name='list-pending-recipients'),
    path('webhook/purchase/', PackagePurchaseWebhookView.as_view(), name='package-purchase-webhook'),
]



