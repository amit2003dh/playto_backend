from django.db import transaction
from django.db.models import Sum, Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from datetime import timedelta
import uuid

from .models import Merchant, Transaction, Payout


class PayoutCreateView(APIView):
    """Handle payout creation with idempotency and concurrency control"""
    def post(self, request):
        idempotency_key = request.headers.get('Idempotency-Key')
        merchant_id = request.data.get('merchant_id')

        # First, make sure they sent an idempotency key
        if not idempotency_key:
            return Response({'error': 'Idempotency-Key header required'}, status=400)

        # Make sure it's actually a valid UUID
        try:
            uuid.UUID(idempotency_key)
        except ValueError:
            return Response({'error': 'Idempotency-Key must be a valid UUID'}, status=400)

        # Check if we've already seen this key in the last 24 hours
        # If yes, return the exact same response as the first time
        existing = Payout.objects.filter(
            merchant_id=merchant_id,
            idempotency_key=idempotency_key,
            idempotency_created_at__gte=timezone.now() - timedelta(hours=24)
        ).first()

        if existing:
            return Response(existing.idempotency_response, status=200)

        amount_paise = request.data.get('amount_paise')
        bank_account_id = request.data.get('bank_account_id')

        # This is the critical part - use SELECT FOR UPDATE to lock the merchant row
        # This prevents two concurrent requests from both thinking they have enough money
        with transaction.atomic():
            try:
                merchant = Merchant.objects.select_for_update().get(id=merchant_id)
            except Merchant.DoesNotExist:
                return Response({'error': 'Merchant not found'}, status=404)

            # Calculate how much money they actually have available
            # We do this inside the lock so nobody can change it while we're checking
            credit_sum = merchant.transactions.aggregate(
                total=Sum('amount_paise', filter=Q(transaction_type='credit'))
            )['total'] or 0

            debit_sum = merchant.transactions.aggregate(
                total=Sum('amount_paise', filter=Q(transaction_type='debit'))
            )['total'] or 0

            available = credit_sum - debit_sum

            # If they don't have enough, reject the request
            if available < amount_paise:
                return Response({
                    'error': 'Insufficient balance',
                    'available_paise': available,
                    'requested_paise': amount_paise,
                }, status=422)

            # Create the payout record
            payout = Payout.objects.create(
                merchant=merchant,
                amount_paise=amount_paise,
                bank_account_id=bank_account_id,
                status='pending',
                idempotency_key=idempotency_key,
            )

            # Create a debit transaction to hold the money
            # This is what makes the money "held" instead of available
            Transaction.objects.create(
                merchant=merchant,
                amount_paise=amount_paise,
                transaction_type='debit',
                description=f'Payout hold #{payout.id}',
                payout=payout,
            )

            # Send the payout to the background worker for processing
            try:
                from .tasks import process_payout
                result = process_payout.delay(payout.id)
                print(f"Task enqueued: {result.id}")
            except Exception as e:
                import traceback
                print(f"Failed to enqueue Celery task: {e}")
                traceback.print_exc()
                # If Celery fails, the payout stays in pending - that's okay
                pass

            response_data = {
                'id': payout.id,
                'amount_paise': payout.amount_paise,
                'status': payout.status,
                'bank_account_id': payout.bank_account_id,
                'created_at': payout.created_at.isoformat(),
            }

            # Save the response so we can return it if they retry with the same key
            payout.idempotency_response = response_data
            payout.save(update_fields=['idempotency_response'])

            return Response(response_data, status=201)


class MerchantBalanceView(APIView):
    """Get a merchant's current balance breakdown"""
    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        return Response({
            'merchant_id': merchant.id,
            'name': merchant.name,
            'available_paise': merchant.available_balance(),  # Money they can withdraw now
            'held_paise': merchant.held_balance(),  # Money tied up in pending payouts
            'total_paise': merchant.available_balance() + merchant.held_balance(),  # Everything they own
        })


class TransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'


class MerchantTransactionsView(APIView):
    """List all transactions for a merchant, paginated"""
    pagination_class = TransactionPagination

    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        transactions = merchant.transactions.all().order_by('-created_at')  # Newest first

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(transactions, request)

        data = []
        for txn in page:
            item = {
                'id': txn.id,
                'amount_paise': txn.amount_paise,
                'transaction_type': txn.transaction_type,
                'description': txn.description,
                'created_at': txn.created_at.isoformat(),
            }
            # If it's a debit tied to a payout, include the payout status
            if txn.transaction_type == 'debit' and txn.payout:
                item['payout_status'] = txn.payout.status
            data.append(item)

        return paginator.get_paginated_response(data)


class PayoutPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'


class MerchantPayoutsView(APIView):
    """List all payouts for a merchant, paginated"""
    pagination_class = PayoutPagination

    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        payouts = merchant.payouts.all().order_by('-created_at')  # Newest first

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(payouts, request)

        data = []
        for payout in page:
            data.append({
                'id': payout.id,
                'amount_paise': payout.amount_paise,
                'status': payout.status,
                'bank_account_id': payout.bank_account_id,
                'attempts': payout.attempts,  # How many times we tried to process it
                'created_at': payout.created_at.isoformat(),
                'updated_at': payout.updated_at.isoformat(),
            })

        return paginator.get_paginated_response(data)


class MerchantsListView(APIView):
    """List all merchants in the system"""
    def get(self, request):
        merchants = Merchant.objects.all().order_by('name')
        data = [{
            'id': m.id,
            'name': m.name,
            'email': m.email,
            'bank_account': m.bank_account,
        } for m in merchants]
        return Response(data)
