import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from payout.models import Merchant, Transaction, Payout

merchants = [
    {'name': 'Acme Design Studio', 'email': 'acme@test.com', 'bank': 'HDFC0001234'},
    {'name': 'Bolt Freelancers', 'email': 'bolt@test.com', 'bank': 'ICIC0005678'},
    {'name': 'Cedar Agency', 'email': 'cedar@test.com', 'bank': 'SBIN0009012'},
]

credits = [
    (0, 25000000, 'Client payment - Stripe Inc'),
    (0, 15000000, 'Client payment - Notion'),
    (1, 8000000, 'Client payment - Figma'),
    (1, 12000000, 'Client payment - Linear'),
    (2, 50000000, 'Client payment - Vercel'),
]

# Create merchants
merchant_objs = []
for m in merchants:
    merchant, created = Merchant.objects.get_or_create(
        email=m['email'],
        defaults={'name': m['name'], 'bank_account': m['bank']}
    )
    merchant_objs.append(merchant)
    print(f"{'Created' if created else 'Found'} merchant: {merchant.name}")

# Create credits
for idx, amount, description in credits:
    merchant = merchant_objs[idx]
    txn, created = Transaction.objects.get_or_create(
        merchant=merchant,
        description=description,
        defaults={
            'amount_paise': amount,
            'transaction_type': 'credit',
        }
    )
    print(f"{'Created' if created else 'Found'} credit: {description} - ₹{amount/100:.2f}")

# Simulate some completed payouts for history
if merchant_objs[0].available_balance() >= 5000000:
    payout = Payout.objects.create(
        merchant=merchant_objs[0],
        amount_paise=5000000,
        bank_account_id=merchant_objs[0].bank_account,
        status='completed',
        idempotency_key='seed-completed-1',
    )
    Transaction.objects.create(
        merchant=merchant_objs[0],
        amount_paise=5000000,
        transaction_type='debit',
        description='Payout completed #1',
        payout=payout,
    )
    print(f"Created completed payout: ₹{5000000/100:.2f}")

print("\nSeed data created successfully!")
