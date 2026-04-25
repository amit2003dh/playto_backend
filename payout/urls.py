from django.urls import path
from .views import (
    PayoutCreateView,
    MerchantBalanceView,
    MerchantTransactionsView,
    MerchantPayoutsView,
    MerchantsListView,
)

urlpatterns = [
    path('payouts/', PayoutCreateView.as_view(), name='payout-create'),
    path('merchants/', MerchantsListView.as_view(), name='merchants-list'),
    path('merchants/<int:merchant_id>/balance/', MerchantBalanceView.as_view(), name='merchant-balance'),
    path('merchants/<int:merchant_id>/transactions/', MerchantTransactionsView.as_view(), name='merchant-transactions'),
    path('merchants/<int:merchant_id>/payouts/', MerchantPayoutsView.as_view(), name='merchant-payouts'),
]
