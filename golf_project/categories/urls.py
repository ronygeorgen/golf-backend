from django.urls import path

from .views import ActiveServiceCategoryListView, CategorySlotsView

urlpatterns = [
    path('active/', ActiveServiceCategoryListView.as_view(), name='categories-active'),
    path('<int:pk>/slots/', CategorySlotsView.as_view(), name='category-slots'),
]
