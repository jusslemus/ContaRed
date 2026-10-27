from django.urls import path

from contared_api.banking import (
    AccountDetailView,
    AccountListCreateView,
    BankDetailView,
    BankListCreateView,
    CurrencyListView,
)
from contared_api.conciliaciones import (
    ReconciliationAccountListView,
    ReconciliationDetailView,
    ReconciliationImportProfileListView,
    ReconciliationListCreateView,
    ReconciliationMovementsView,
    ReconciliationPreviewConfirmView,
    ReconciliationPreviewCreateView,
    ReconciliationPreviewInspectView,
    ReconciliationPreviewValidateView,
)
from contared_api.views import ApiStatusView, SessionView

urlpatterns = [
    path("api/v1/status/", ApiStatusView.as_view(), name="api-status"),
    path("api/v1/session/", SessionView.as_view(), name="api-session"),
    path("api/v1/bancos/", BankListCreateView.as_view(), name="api-banks"),
    path("api/v1/bancos/<uuid:bank_id>/", BankDetailView.as_view(), name="api-bank-detail"),
    path("api/v1/cuentas/", AccountListCreateView.as_view(), name="api-accounts"),
    path("api/v1/cuentas/<uuid:account_id>/", AccountDetailView.as_view(), name="api-account-detail"),
    path("api/v1/monedas/", CurrencyListView.as_view(), name="api-currencies"),
    path("api/v1/conciliaciones/cuentas/", ReconciliationAccountListView.as_view(), name="api-reconciliation-accounts"),
    path("api/v1/conciliaciones/", ReconciliationListCreateView.as_view(), name="api-reconciliations"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/", ReconciliationDetailView.as_view(), name="api-reconciliation-detail"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/movimientos/", ReconciliationMovementsView.as_view(), name="api-reconciliation-movements"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/perfiles/", ReconciliationImportProfileListView.as_view(), name="api-reconciliation-profiles"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/previsualizaciones/", ReconciliationPreviewCreateView.as_view(), name="api-reconciliation-preview"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/previsualizaciones/<uuid:preview_id>/inspeccionar/", ReconciliationPreviewInspectView.as_view(), name="api-reconciliation-preview-inspect"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/previsualizaciones/<uuid:preview_id>/validar/", ReconciliationPreviewValidateView.as_view(), name="api-reconciliation-preview-validate"),
    path("api/v1/conciliaciones/<uuid:reconciliation_id>/previsualizaciones/<uuid:preview_id>/confirmar/", ReconciliationPreviewConfirmView.as_view(), name="api-reconciliation-preview-confirm"),
]
