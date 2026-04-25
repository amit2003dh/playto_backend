from django.test import TestCase, TransactionTestCase
from concurrent.futures import ThreadPoolExecutor
from rest_framework.test import APIClient
from .models import Merchant, Transaction, Payout


class ConcurrencyTest(TransactionTestCase):
    """Use TransactionTestCase so real DB transactions are tested."""

    def setUp(self):
        self.merchant = Merchant.objects.create(
            name='Test Merchant', email='test@test.com', bank_account='HDFC001'
        )
        Transaction.objects.create(
            merchant=self.merchant,
            amount_paise=10000,  # 100 rupees
            transaction_type='credit',
            description='Seed credit',
        )
        self.client = APIClient()

    def test_only_one_of_two_concurrent_payouts_succeeds(self):
        results = []

        def make_request(key):
            response = self.client.post('/api/v1/payouts/', {
                'merchant_id': self.merchant.id,
                'amount_paise': 6000,  # 60 rupees
                'bank_account_id': 'HDFC001',
            }, HTTP_IDEMPOTENCY_KEY=key, format='json')
            results.append(response.status_code)

        with ThreadPoolExecutor(max_workers=2) as executor:
            executor.submit(make_request, 'key-aaa-111')
            executor.submit(make_request, 'key-bbb-222')

        success_count = results.count(201)
        failure_count = results.count(422)
        self.assertEqual(success_count, 1)
        self.assertEqual(failure_count, 1)

        # Verify ledger invariant: balance should be 40 rupees held
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.held_balance(), 6000)
        self.assertEqual(self.merchant.available_balance(), 4000)


class IdempotencyTest(TestCase):
    def test_same_idempotency_key_returns_same_response(self):
        merchant = Merchant.objects.create(
            name='Idem Merchant', email='idem@test.com', bank_account='SBI001'
        )
        Transaction.objects.create(
            merchant=merchant, amount_paise=50000,
            transaction_type='credit', description='Seed'
        )
        client = APIClient()
        key = 'test-idem-key-12345'

        resp1 = client.post('/api/v1/payouts/', {
            'merchant_id': merchant.id,
            'amount_paise': 10000,
            'bank_account_id': 'SBI001',
        }, HTTP_IDEMPOTENCY_KEY=key, format='json')

        resp2 = client.post('/api/v1/payouts/', {
            'merchant_id': merchant.id,
            'amount_paise': 10000,
            'bank_account_id': 'SBI001',
        }, HTTP_IDEMPOTENCY_KEY=key, format='json')

        self.assertEqual(resp1.status_code, 201)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.data['id'], resp2.data['id'])

        # Only one payout should exist
        self.assertEqual(Payout.objects.filter(
            merchant=merchant, idempotency_key=key
        ).count(), 1)

        # Only one debit should exist
        self.assertEqual(Transaction.objects.filter(
            merchant=merchant, transaction_type='debit'
        ).count(), 1)
