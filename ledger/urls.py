from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ContactViewSet, DashboardView, LedgerEntryViewSet, SummaryView

router = DefaultRouter()
router.register(r'contacts', ContactViewSet, basename='ledger-contact')
router.register(r'entries', LedgerEntryViewSet, basename='ledger-entry')

urlpatterns = [
    path('summary/', SummaryView.as_view(), name='ledger-summary'),
    path('dashboard/', DashboardView.as_view(), name='ledger-dashboard'),
    path('', include(router.urls)),
]
