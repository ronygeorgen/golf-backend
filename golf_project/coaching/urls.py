from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import CoachingPackageViewSet

router = DefaultRouter()
router.register(r'packages', CoachingPackageViewSet, basename='coaching-package')

urlpatterns = [
    path('', include(router.urls)),
]



