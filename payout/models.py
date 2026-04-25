from django.db import models
from django.db.models import Sum, Q

TRANSACTION_TYPES = [('credit', 'Credit'), ('debit', 'Debit')]

PAYOUT_STATUS = [
    ('pending', 'Pending'),
    ('processing', 'Processing'),
    ('completed', 'Completed'),
    ('failed', 'Failed'),
]

ALLOWED_TRANSITIONS = {
    'pending': ['processing'],
    'processing': ['completed', 'failed'],
    'completed': [],
    'failed': [],
}


class Merchant(models.Model):
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    bank_account = models.CharField(max_length=50)  # simulated IFSC+account
    created_at = models.DateTimeField(auto_now_add=True)

    def available_balance(self):
        # IMPORTANT: Must use DB-level aggregation, NOT Python arithmetic
        # Sum credits minus sum of non-held debits
        credit_sum = self.transactions.aggregate(
            total=Sum('amount_paise', filter=Q(transaction_type='credit'))
        )['total'] or 0

        debit_sum = self.transactions.aggregate(
            total=Sum('amount_paise', filter=Q(
                transaction_type='debit',
                payout__status__in=['pending', 'processing', 'completed']
            ))
        )['total'] or 0

        return credit_sum - debit_sum

    def held_balance(self):
        result = self.transactions.aggregate(
            held=Sum(
                'amount_paise',
                filter=Q(transaction_type='debit') & Q(
                    payout__status__in=['pending', 'processing']
                )
            )
        )
        return result['held'] or 0


class Transaction(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='transactions')
    amount_paise = models.BigIntegerField()  # NEVER FloatField or DecimalField
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255)
    payout = models.ForeignKey('Payout', null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['merchant', 'created_at'])]


class Payout(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='payouts')
    amount_paise = models.BigIntegerField()
    bank_account_id = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=PAYOUT_STATUS, default='pending')
    idempotency_key = models.CharField(max_length=64)
    idempotency_response = models.JSONField(null=True, blank=True)  # cached response
    idempotency_created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.IntegerField(default=0)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('merchant', 'idempotency_key')]
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['merchant', 'idempotency_key']),
        ]

    def transition_to(self, new_status):
        """Enforce state machine. Raise ValueError on illegal transition."""
        allowed = ALLOWED_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Illegal transition: {self.status} → {new_status}. "
                f"Allowed from {self.status}: {allowed}"
            )
        self.status = new_status
