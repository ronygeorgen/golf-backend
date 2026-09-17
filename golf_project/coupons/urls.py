from django.urls import path
from .views import (
    CouponListCreateView,
    CouponDetailView,
    CouponUsageListView,
    CouponValidateView,
    CouponQuickCreateView,
)

urlpatterns = [
    # Public (authenticated users)
    path('validate/', CouponValidateView.as_view(), name='coupon-validate'),
    # Staff / admin: one-off discount for Quick Checkout
    path('quick-create/', CouponQuickCreateView.as_view(), name='coupon-quick-create'),

    # Admin only
    path('', CouponListCreateView.as_view(), name='coupon-list-create'),
    path('<int:pk>/', CouponDetailView.as_view(), name='coupon-detail'),
    path('usages/', CouponUsageListView.as_view(), name='coupon-usages'),
]
