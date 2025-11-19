from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'simulators', views.SimulatorViewSet, basename='simulator')
router.register(r'duration-prices', views.DurationPriceViewSet, basename='duration-price')
router.register(r'credits', views.SimulatorCreditViewSet, basename='simulator-credit')

urlpatterns = [
    path('', include(router.urls)),
]


