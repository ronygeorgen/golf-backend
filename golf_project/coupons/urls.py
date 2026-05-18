from django.urls import path
from .views import CouponListCreateView, CouponDetailView, CouponUsageListView, CouponValidateView

urlpatterns = [
    # Public (authenticated users)
    path('validate/', CouponValidateView.as_view(), name='coupon-validate'),

    # Admin only
    path('', CouponListCreateView.as_view(), name='coupon-list-create'),
    path('<int:pk>/', CouponDetailView.as_view(), name='coupon-detail'),
    path('usages/', CouponUsageListView.as_view(), name='coupon-usages'),
]
