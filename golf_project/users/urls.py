from django.urls import path
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()

urlpatterns = [
    # Authentication endpoints
    path('signup/', views.signup, name='signup'),
    path('login/', views.login, name='login'),
    path('logout/', views.logout, name='logout'),
    path('profile/', views.profile, name='profile'),
    
    # OTP authentication endpoints
    path('request-otp/', views.request_otp, name='request_otp'),
    path('verify-otp/', views.verify_otp, name='verify_otp'),
] + router.urls

