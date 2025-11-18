from django.urls import path, include
from rest_framework.routers import DefaultRouter
from admin_panel.views import AdminDashboardViewSet, StaffViewSet
from simulators.views import SimulatorViewSet, DurationPriceViewSet
from coaching.views import CoachingPackageViewSet
from bookings.views import BookingViewSet

router = DefaultRouter()
router.register(r'dashboard', AdminDashboardViewSet, basename='admin-dashboard')
router.register(r'staff', StaffViewSet, basename='admin-staff')
router.register(r'simulators', SimulatorViewSet, basename='admin-simulators')
router.register(r'duration-prices', DurationPriceViewSet, basename='admin-duration-prices')
router.register(r'packages', CoachingPackageViewSet, basename='admin-packages')
router.register(r'bookings', BookingViewSet, basename='admin-bookings')

urlpatterns = [
    path('', include(router.urls)),
]