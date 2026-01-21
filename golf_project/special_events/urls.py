from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SpecialEventViewSet, 
    SpecialEventRegistrationViewSet, 
    SpecialEventWebhookView
)

router = DefaultRouter()
router.register(r'events', SpecialEventViewSet, basename='special-event')
router.register(r'registrations', SpecialEventRegistrationViewSet, basename='special-event-registration')

urlpatterns = [
    path('webhook/', SpecialEventWebhookView.as_view(), name='special-event-webhook'),
    path('', include(router.urls)),
]



