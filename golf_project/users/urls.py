from django.urls import path
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()

urlpatterns = [
    # Authentication endpoints
    path('signup/', views.signup, name='signup'),
    path('signup-without-otp/', views.signup_without_otp, name='signup_without_otp'),
    path('login/', views.login, name='login'),
    path('logout/', views.logout, name='logout'),
    path('profile/', views.profile, name='profile'),
    path('auto-login/', views.auto_login, name='auto_login'),
    
    # OTP authentication endpoints
    path('request-otp/', views.request_otp, name='request_otp'),
    path('verify-otp/', views.verify_otp, name='verify_otp'),
    
    # GHL locations endpoint for signup
    path('ghl-locations/', views.list_ghl_locations, name='list_ghl_locations'),
    
    # Profile endpoints
    path('update-dob/', views.update_dob, name='update_dob'),
    
    # Member list endpoint (staff/admin only)
    path('member-list/', views.member_list, name='member_list'),
] + router.urls

