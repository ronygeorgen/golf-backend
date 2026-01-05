from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'', views.BookingViewSet, basename='booking')

urlpatterns = [
    path('temp-booking/', views.CreateTempBookingView.as_view(), name='create-temp-booking'),
    path('webhook/booking/', views.BookingWebhookView.as_view(), name='booking-webhook'),
    path('guest-create/', views.GuestBookingCreateView.as_view(), name='guest-booking-create'),
    path('', include(router.urls)),
]
