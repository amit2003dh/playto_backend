from django.contrib import admin
from .models import Merchant, Transaction, Payout


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'bank_account', 'created_at']
    search_fields = ['name', 'email']


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'amount_paise', 'transaction_type', 'description', 'payout', 'created_at']
    list_filter = ['transaction_type', 'created_at']
    search_fields = ['merchant__name', 'description']


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'amount_paise', 'status', 'attempts', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['merchant__name', 'idempotency_key']
