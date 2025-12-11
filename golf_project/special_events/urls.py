from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SpecialEventViewSet, SpecialEventRegistrationViewSet

router = DefaultRouter()
router.register(r'events', SpecialEventViewSet, basename='special-event')
router.register(r'registrations', SpecialEventRegistrationViewSet, basename='special-event-registration')

urlpatterns = [
    path('', include(router.urls)),
]


