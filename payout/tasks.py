import random
import time
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta


@shared_task(bind=True, max_retries=3)
def process_payout(self, payout_id):
    """
    Background task that processes a payout through the simulated bank transfer.
    This is where the actual "work" happens - moving money from pending to completed/failed.
    """
    from .models import Payout, Transaction

    # First transaction: move the payout from pending to processing
    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(id=payout_id)
        except Payout.DoesNotExist:
            return  # Payout was deleted, nothing to do

        # Only process payouts that are still pending
        # If it's already processing or done, another worker got to it first
        if payout.status != 'pending':
            return

        # Try to move to processing state
        # This validates the state machine - can't go backwards
        try:
            payout.transition_to('processing')
        except ValueError as e:
            return  # Another worker already moved it

        # Track how many times we've tried
        payout.attempts += 1
        payout.last_attempted_at = timezone.now()
        payout.save(update_fields=['status', 'attempts', 'last_attempted_at'])

    # Wait a bit so the UI can show the "processing" state
    # This makes the state transitions visible to the user
    time.sleep(3)

    # Simulate calling the bank API
    # We do this OUTSIDE the transaction because I/O (network calls) should never hold DB locks
    outcome = simulate_bank_transfer(payout.amount_paise)

    # Second transaction: update the final status based on bank response
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(id=payout_id)

        if outcome == 'success':
            # Money sent successfully - mark as completed
            payout.transition_to('completed')
            payout.save(update_fields=['status', 'updated_at'])

        elif outcome == 'failed':
            # Bank rejected the transfer - mark as failed and refund the money
            payout.transition_to('failed')
            payout.save(update_fields=['status', 'updated_at'])
            # Create a credit transaction to return the money to the merchant
            # This happens atomically with the status change
            Transaction.objects.create(
                merchant=payout.merchant,
                amount_paise=payout.amount_paise,
                transaction_type='credit',
                description=f'Payout failed refund #{payout.id}',
                payout=payout,
            )

        elif outcome == 'hung':
            # Bank didn't respond - leave it in processing
            # The retry task will pick this up and try again
            pass


def simulate_bank_transfer(amount_paise):
    """
    Pretend to call a bank API to transfer money.
    Returns: 'success' (70%), 'failed' (20%), or 'hung' (10%)
    This simulates real-world bank API behavior where things can go wrong.
    """
    roll = random.random()
    if roll < 0.70:
        return 'success'  # Most transfers work
    elif roll < 0.90:
        return 'failed'  # Some fail (insufficient funds, wrong account, etc.)
    else:
        time.sleep(0.1)  # Simulate a delay
        return 'hung'  # Bank doesn't respond (network issue, timeout, etc.)


@shared_task
def retry_stuck_payouts():
    """
    Periodic task that runs every 30 seconds (configured in celery.py).
    It finds payouts that are stuck in 'processing' for too long and retries them.
    After 3 failed attempts, it gives up and refunds the money.
    """
    from .models import Payout, Transaction
    from django.db import transaction as db_transaction

    # Find payouts that have been in 'processing' for more than 30 seconds
    cutoff = timezone.now() - timedelta(seconds=30)
    stuck = Payout.objects.filter(
        status='processing',
        last_attempted_at__lt=cutoff,
    )

    for payout in stuck:
        with db_transaction.atomic():
            payout = Payout.objects.select_for_update().get(id=payout.id)
            # Double-check it's still processing (might have changed)
            if payout.status != 'processing':
                continue

            if payout.attempts >= 3:
                # We've tried 3 times and it's still stuck - give up
                # Mark as failed and refund the money
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
                # Haven't hit max retries yet - try again
                # Reset to pending and re-enqueue the task
                payout.status = 'pending'
                payout.save(update_fields=['status', 'updated_at'])
                # Use exponential backoff: wait 2^attempts seconds before retrying
                # This prevents hammering the bank if it's having issues
                process_payout.apply_async(
                    args=[payout.id],
                    countdown=2 ** payout.attempts
                )
