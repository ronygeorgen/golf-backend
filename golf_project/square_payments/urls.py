from django.urls import path
from .views import InitiateSquarePaymentView, SquareWebhookView, SquareConfigView

urlpatterns = [
    path('initiate-payment/', InitiateSquarePaymentView.as_view(), name='square-initiate-payment'),
    path('webhook/', SquareWebhookView.as_view(), name='square-webhook'),
    path('config/', SquareConfigView.as_view(), name='square-config'),
]
