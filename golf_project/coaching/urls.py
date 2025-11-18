from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import CoachingPackageViewSet, CoachingPackagePurchaseViewSet

router = DefaultRouter()
router.register(r'packages', CoachingPackageViewSet, basename='coaching-package')
router.register(r'purchases', CoachingPackagePurchaseViewSet, basename='coaching-purchase')

urlpatterns = [
    path('', include(router.urls)),
]



