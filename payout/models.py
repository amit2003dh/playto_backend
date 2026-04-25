from django.db import models
from django.db.models import Sum, Q

# Transaction types - money coming in (credit) or going out (debit)
TRANSACTION_TYPES = [('credit', 'Credit'), ('debit', 'Debit')]

# Payout lifecycle states - can only move forward, never backwards
PAYOUT_STATUS = [
    ('pending', 'Pending'),        # Just created, waiting to be picked up
    ('processing', 'Processing'),  # Worker is processing it
    ('completed', 'Completed'),    # Money sent successfully
    ('failed', 'Failed'),         # Something went wrong, money refunded
]

# Define which state transitions are allowed
# This prevents payouts from going backwards or jumping around
ALLOWED_TRANSITIONS = {
    'pending': ['processing'],           # Can only go from pending to processing
    'processing': ['completed', 'failed'], # From processing, can succeed or fail
    'completed': [],                     # Once done, can't change
    'failed': [],                        # Once failed, can't change
}


class Merchant(models.Model):
    """A merchant who receives payments and requests payouts"""
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    bank_account = models.CharField(max_length=50)  # simulated IFSC+account
    created_at = models.DateTimeField(auto_now_add=True)

    def available_balance(self):
        """
        Calculate how much money the merchant can actually withdraw right now.
        This is all credits minus all debits (even failed ones).
        We subtract ALL debits because when a payout fails, the debit stays
        in the ledger (money was held) and a refund credit is added.
        """
        credit_sum = self.transactions.aggregate(
            total=Sum('amount_paise', filter=Q(transaction_type='credit'))
        )['total'] or 0

        debit_sum = self.transactions.aggregate(
            total=Sum('amount_paise', filter=Q(transaction_type='debit'))
        )['total'] or 0

        return credit_sum - debit_sum

    def held_balance(self):
        """
        Calculate how much money is currently being held for pending/processing payouts.
        This money can't be withdrawn until the payout completes or fails.
        """
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
    """A single credit or debit in the merchant's ledger"""
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='transactions')
    amount_paise = models.BigIntegerField()  # Always use integers for money, never floats
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    description = models.CharField(max_length=255)
    payout = models.ForeignKey('Payout', null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['merchant', 'created_at'])]  # Speed up queries by merchant and time


class Payout(models.Model):
    """A payout request that goes through the bank transfer process"""
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='payouts')
    amount_paise = models.BigIntegerField()
    bank_account_id = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=PAYOUT_STATUS, default='pending')
    idempotency_key = models.CharField(max_length=64)  # UUID to prevent duplicate payouts
    idempotency_response = models.JSONField(null=True, blank=True)  # Cache the first response for replays
    idempotency_created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.IntegerField(default=0)  # How many times we've tried to process this
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('merchant', 'idempotency_key')]  # One payout per key per merchant
        indexes = [
            models.Index(fields=['status', 'created_at']),  # Speed up queries by status and time
            models.Index(fields=['merchant', 'idempotency_key']),  # Speed up idempotency checks
        ]

    def transition_to(self, new_status):
        """
        Change the payout status, but only if it's a legal transition.
        This prevents payouts from going backwards or jumping around.
        Raises ValueError if you try to do something illegal.
        """
        allowed = ALLOWED_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Illegal transition: {self.status} → {new_status}. "
                f"Allowed from {self.status}: {allowed}"
            )
        self.status = new_status
