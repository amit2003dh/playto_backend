import random
import time
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta


@shared_task(bind=True, max_retries=3)
def process_payout(self, payout_id):
    from .models import Payout, Transaction

    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(id=payout_id)
        except Payout.DoesNotExist:
            return

        # Only process pending payouts
        if payout.status != 'pending':
            return

        # Transition to processing (validates state machine)
        try:
            payout.transition_to('processing')
        except ValueError as e:
            return  # Already moved by another worker

        payout.attempts += 1
        payout.last_attempted_at = timezone.now()
        payout.save(update_fields=['status', 'attempts', 'last_attempted_at'])

    # Simulate bank call OUTSIDE the transaction (I/O should not hold DB locks)
    outcome = simulate_bank_transfer(payout.amount_paise)

    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(id=payout_id)

        if outcome == 'success':
            payout.transition_to('completed')
            payout.save(update_fields=['status', 'updated_at'])

        elif outcome == 'failed':
            payout.transition_to('failed')
            payout.save(update_fields=['status', 'updated_at'])
            # Return funds atomically with the state transition
            Transaction.objects.create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                transaction_type='credit',
                description=f'Payout failed refund #{payout.id}',
                payout=payout,
            )

        elif outcome == 'hung':
            # Leave in 'processing' — retry task handles this
            pass


def simulate_bank_transfer(amount_paise):
    """70% success, 20% fail, 10% hang"""
    roll = random.random()
    if roll < 0.70:
        return 'success'
    elif roll < 0.90:
        return 'failed'
    else:
        time.sleep(0.1)  # simulate delay — real hang is handled by retry
        return 'hung'


@shared_task
def retry_stuck_payouts():
    """
    Beat task: runs every 30 seconds.
    Picks up payouts stuck in 'processing' for >30 seconds.
    Max 3 attempts total, then mark failed and return funds.
    """
    from .models import Payout, Transaction
    from django.db import transaction as db_transaction

    cutoff = timezone.now() - timedelta(seconds=30)
    stuck = Payout.objects.filter(
        status='processing',
        last_attempted_at__lt=cutoff,
    )

    for payout in stuck:
        with db_transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout.id)
            if payout.status != 'processing':
                continue

            if payout.attempts >= 3:
                # Max retries reached — fail it and return funds
                payout.transition_to('failed')
                payout.save(update_fields=['status', 'updated_at'])
                Transaction.objects.create(
                    merchant=payout.merchant,
                    amount_paise=payout.amount_paise,
                    transaction_type='credit',
                    description=f'Payout max-retries refund #{payout.id}',
                    payout=payout,
                )
            else:
                # Reset to pending and re-enqueue
                payout.status = 'pending'
                payout.save(update_fields=['status', 'updated_at'])
                process_payout.apply_async(
                    args=[payout.id],
                    countdown=2 ** payout.attempts  # exponential backoff
                )
