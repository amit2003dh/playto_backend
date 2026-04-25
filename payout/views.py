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
    def post(self, request):
        idempotency_key = request.headers.get('Idempotency-Key')
        merchant_id = request.data.get('merchant_id')

        # Validate idempotency key
        if not idempotency_key:
            return Response({'error': 'Idempotency-Key header required'}, status=400)

        try:
            uuid.UUID(idempotency_key)
        except ValueError:
            return Response({'error': 'Idempotency-Key must be a valid UUID'}, status=400)

        # Check for existing key (scoped per merchant, expires after 24h)
        existing = Payout.objects.filter(
            merchant_id=merchant_id,
            idempotency_key=idempotency_key,
            idempotency_created_at__gte=timezone.now() - timedelta(hours=24)
        ).first()

        if existing:
            # Return exact same response as first call
            return Response(existing.idempotency_response, status=200)

        amount_paise = request.data.get('amount_paise')
        bank_account_id = request.data.get('bank_account_id')

        # CRITICAL: Use SELECT FOR UPDATE to prevent race conditions
        # This is the lock that prevents two concurrent 60-rupee requests on a 100-rupee balance
        with transaction.atomic():
            try:
                merchant = Merchant.objects.select_for_update().get(id=merchant_id)
            except Merchant.DoesNotExist:
                return Response({'error': 'Merchant not found'}, status=404)

            # Calculate available balance at the DB level inside the lock
            credit_sum = merchant.transactions.aggregate(
                total=Sum('amount_paise', filter=Q(transaction_type='credit'))
            )['total'] or 0

            debit_sum = merchant.transactions.aggregate(
                total=Sum('amount_paise', filter=Q(
                    transaction_type='debit',
                    payout__status__in=['pending', 'processing', 'completed']
                ))
            )['total'] or 0

            available = credit_sum - debit_sum

            if available < amount_paise:
                return Response({
                    'error': 'Insufficient balance',
                    'available_paise': available,
                    'requested_paise': amount_paise,
                }, status=422)

            # Create payout and hold funds atomically
            payout = Payout.objects.create(
                merchant=merchant,
                amount_paise=amount_paise,
                bank_account_id=bank_account_id,
                status='pending',
                idempotency_key=idempotency_key,
            )

            # Create debit transaction to hold funds
            Transaction.objects.create(
                merchant=merchant,
                amount_paise=amount_paise,
                transaction_type='debit',
                description=f'Payout hold #{payout.id}',
                payout=payout,
            )

            # Enqueue background job
            try:
                from .tasks import process_payout
                result = process_payout.delay(payout.id)
                print(f"Task enqueued: {result.id}")
            except Exception as e:
                import traceback
                print(f"Failed to enqueue Celery task: {e}")
                traceback.print_exc()
                # Continue without Celery - payout will stay in pending
                pass

            response_data = {
                'id': payout.id,
                'amount_paise': payout.amount_paise,
                'status': payout.status,
                'bank_account_id': payout.bank_account_id,
                'created_at': payout.created_at.isoformat(),
            }

            # Cache response for idempotency replay
            payout.idempotency_response = response_data
            payout.save(update_fields=['idempotency_response'])

            return Response(response_data, status=201)


class MerchantBalanceView(APIView):
    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        return Response({
            'merchant_id': merchant.id,
            'name': merchant.name,
            'available_paise': merchant.available_balance(),
            'held_paise': merchant.held_balance(),
            'total_paise': merchant.available_balance() + merchant.held_balance(),
        })


class TransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'


class MerchantTransactionsView(APIView):
    pagination_class = TransactionPagination

    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        transactions = merchant.transactions.all().order_by('-created_at')
        
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
            if txn.transaction_type == 'debit' and txn.payout:
                item['payout_status'] = txn.payout.status
            data.append(item)
        
        return paginator.get_paginated_response(data)


class PayoutPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'


class MerchantPayoutsView(APIView):
    pagination_class = PayoutPagination

    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        payouts = merchant.payouts.all().order_by('-created_at')
        
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(payouts, request)
        
        data = []
        for payout in page:
            data.append({
                'id': payout.id,
                'amount_paise': payout.amount_paise,
                'status': payout.status,
                'bank_account_id': payout.bank_account_id,
                'attempts': payout.attempts,
                'created_at': payout.created_at.isoformat(),
                'updated_at': payout.updated_at.isoformat(),
            })
        
        return paginator.get_paginated_response(data)


class MerchantsListView(APIView):
    def get(self, request):
        merchants = Merchant.objects.all().order_by('name')
        data = [{
            'id': m.id,
            'name': m.name,
            'email': m.email,
            'bank_account': m.bank_account,
        } for m in merchants]
        return Response(data)
