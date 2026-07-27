"""
ReSell Trust - Webhook Views (core/webhook_views.py)
Handle payment gateway webhooks
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.conf import settings
import stripe
import logging

from .models import Transaction, Reservation, AuctionListing
from .notifications import (
    notify_payment_successful,
    notify_payment_failed,
    notify_refund_processed
)

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


@method_decorator(csrf_exempt, name='dispatch')
class StripeWebhookView(APIView):
    """
    Handle Stripe webhook events
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {str(e)}")
            return Response({'error': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Invalid signature: {str(e)}")
            return Response({'error': 'Invalid signature'}, status=status.HTTP_400_BAD_REQUEST)

        # Handle the event
        event_type = event['type']
        event_data = event['data']['object']

        logger.info(f"Received Stripe webhook: {event_type}")

        # Payment Intent Events
        if event_type == 'payment_intent.succeeded':
            self.handle_payment_success(event_data)

        elif event_type == 'payment_intent.payment_failed':
            self.handle_payment_failed(event_data)

        # Charge Events
        elif event_type == 'charge.succeeded':
            self.handle_charge_success(event_data)

        elif event_type == 'charge.failed':
            self.handle_charge_failed(event_data)

        # Refund Events
        elif event_type == 'charge.refunded':
            self.handle_refund(event_data)

        # Payout Events
        elif event_type == 'payout.paid':
            self.handle_payout_paid(event_data)

        elif event_type == 'payout.failed':
            self.handle_payout_failed(event_data)

        # Dispute Events
        elif event_type == 'charge.dispute.created':
            self.handle_dispute_created(event_data)

        else:
            logger.info(f"Unhandled event type: {event_type}")

        return Response({'status': 'success'})

    def handle_payment_success(self, payment_intent):
        """Handle successful payment"""
        try:
            payment_intent_id = payment_intent['id']
            amount = payment_intent['amount'] / 100  # Convert from cents

            # Find transaction
            transaction = Transaction.objects.filter(
                stripe_payment_intent_id=payment_intent_id
            ).first()

            if transaction:
                transaction.status = 'completed'
                transaction.save()

                # Send notification
                notify_payment_successful(transaction)

                # Handle reservation completion
                if transaction.reservation:
                    reservation = transaction.reservation
                    reservation.status = 'completed'
                    reservation.save()

                    # Update item status
                    item = reservation.item
                    item.status = 'sold'
                    item.save()

                logger.info(f"Payment successful: {payment_intent_id}")

        except Exception as e:
            logger.error(f"Error handling payment success: {str(e)}")

    def handle_payment_failed(self, payment_intent):
        """Handle failed payment"""
        try:
            payment_intent_id = payment_intent['id']

            transaction = Transaction.objects.filter(
                stripe_payment_intent_id=payment_intent_id
            ).first()

            if transaction:
                transaction.status = 'failed'
                transaction.save()

                notify_payment_failed(transaction)

                logger.info(f"Payment failed: {payment_intent_id}")

        except Exception as e:
            logger.error(f"Error handling payment failure: {str(e)}")

    def handle_charge_success(self, charge):
        """Handle successful charge"""
        try:
            charge_id = charge['id']
            amount = charge['amount'] / 100

            logger.info(f"Charge successful: {charge_id} - ${amount}")

        except Exception as e:
            logger.error(f"Error handling charge success: {str(e)}")

    def handle_charge_failed(self, charge):
        """Handle failed charge"""
        try:
            charge_id = charge['id']
            failure_message = charge.get('failure_message', 'Unknown error')

            logger.warning(f"Charge failed: {charge_id} - {failure_message}")

        except Exception as e:
            logger.error(f"Error handling charge failure: {str(e)}")

    def handle_refund(self, charge):
        """Handle refund"""
        try:
            charge_id = charge['id']
            refunds = charge.get('refunds', {}).get('data', [])

            for refund in refunds:
                refund_amount = refund['amount'] / 100

                # Find original transaction
                transaction = Transaction.objects.filter(
                    stripe_payment_intent_id=charge.get('payment_intent')
                ).first()

                if transaction:
                    # Create refund transaction
                    refund_transaction = Transaction.objects.create(
                        user=transaction.user,
                        transaction_type='refund',
                        amount=refund_amount,
                        status='completed',
                        item=transaction.item,
                        reservation=transaction.reservation,
                        auction=transaction.auction
                    )

                    notify_refund_processed(refund_transaction)

                    logger.info(f"Refund processed: ${refund_amount}")

        except Exception as e:
            logger.error(f"Error handling refund: {str(e)}")

    def handle_payout_paid(self, payout):
        """Handle successful payout"""
        try:
            payout_id = payout['id']
            amount = payout['amount'] / 100

            logger.info(f"Payout successful: {payout_id} - ${amount}")

        except Exception as e:
            logger.error(f"Error handling payout: {str(e)}")

    def handle_payout_failed(self, payout):
        """Handle failed payout"""
        try:
            payout_id = payout['id']
            failure_message = payout.get('failure_message', 'Unknown error')

            logger.warning(f"Payout failed: {payout_id} - {failure_message}")

        except Exception as e:
            logger.error(f"Error handling payout failure: {str(e)}")

    def handle_dispute_created(self, dispute):
        """Handle dispute created"""
        try:
            dispute_id = dispute['id']
            amount = dispute['amount'] / 100
            reason = dispute.get('reason', 'Unknown')

            logger.warning(f"Dispute created: {dispute_id} - ${amount} - Reason: {reason}")

            # You can send notification to admin here

        except Exception as e:
            logger.error(f"Error handling dispute: {str(e)}")


class StripePaymentService:
    """
    Service class for Stripe payment operations
    """

    @staticmethod
    def create_payment_intent(amount, currency='usd', metadata=None):
        """
        Create a Stripe payment intent
        """
        try:
            intent = stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Convert to cents
                currency=currency,
                metadata=metadata or {},
                automatic_payment_methods={'enabled': True},
            )
            return intent
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error: {str(e)}")
            raise

    @staticmethod
    def create_deposit_payment(user, amount, item_id, reservation_id=None, auction_id=None):
        """
        Create payment intent for deposit
        """
        metadata = {
            'user_id': user.id,
            'item_id': item_id,
            'type': 'deposit'
        }

        if reservation_id:
            metadata['reservation_id'] = reservation_id
        if auction_id:
            metadata['auction_id'] = auction_id

        intent = StripePaymentService.create_payment_intent(amount, metadata=metadata)

        # Create transaction record
        from .models import Item, Reservation, AuctionListing

        transaction = Transaction.objects.create(
            user=user,
            transaction_type='deposit',
            amount=amount,
            status='pending',
            item_id=item_id,
            reservation_id=reservation_id,
            auction_id=auction_id,
            stripe_payment_intent_id=intent.id
        )

        return intent, transaction

    @staticmethod
    def create_final_payment(user, reservation):
        """
        Create payment intent for final payment
        """
        remaining_amount = reservation.total_price - reservation.deposit_amount

        metadata = {
            'user_id': user.id,
            'item_id': reservation.item.id,
            'reservation_id': reservation.id,
            'type': 'final_payment'
        }

        intent = StripePaymentService.create_payment_intent(remaining_amount, metadata=metadata)

        transaction = Transaction.objects.create(
            user=user,
            transaction_type='payment',
            amount=remaining_amount,
            status='pending',
            item=reservation.item,
            reservation=reservation,
            stripe_payment_intent_id=intent.id
        )

        return intent, transaction

    @staticmethod
    def refund_payment(payment_intent_id, amount=None):
        """
        Refund a payment
        """
        try:
            refund = stripe.Refund.create(
                payment_intent=payment_intent_id,
                amount=int(amount * 100) if amount else None
            )
            return refund
        except stripe.error.StripeError as e:
            logger.error(f"Refund error: {str(e)}")
            raise

    @staticmethod
    def create_connect_account(user):
        """
        Create a Stripe Connect account for seller payouts
        """
        try:
            account = stripe.Account.create(
                type='express',
                country='US',
                email=user.email,
                capabilities={
                    'card_payments': {'requested': True},
                    'transfers': {'requested': True},
                },
                business_type='individual',
                metadata={'user_id': user.id}
            )
            return account
        except stripe.error.StripeError as e:
            logger.error(f"Connect account error: {str(e)}")
            raise

    @staticmethod
    def create_transfer(destination_account_id, amount, metadata=None):
        """
        Transfer funds to a Connect account (seller payout)
        """
        try:
            transfer = stripe.Transfer.create(
                amount=int(amount * 100),
                currency='usd',
                destination=destination_account_id,
                metadata=metadata or {},
            )
            return transfer
        except stripe.error.StripeError as e:
            logger.error(f"Transfer error: {str(e)}")
            raise