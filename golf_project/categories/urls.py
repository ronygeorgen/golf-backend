from django.urls import path

from .views import ActiveServiceCategoryListView

urlpatterns = [
    path('active/', ActiveServiceCategoryListView.as_view(), name='categories-active'),
]
