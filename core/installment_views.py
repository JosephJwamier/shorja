"""HTTP surface for installments.

Four endpoints, none of them financier-specific:

    GET  /api/installments/options/          what the app may offer, and the terms
    POST /api/installments/check/            card + order -> eligibility and schedule
    POST /api/installments/confirm/          commit the plan, complete the order
    POST /api/installments/webhook/<name>/   financier callback

The flow is two calls on purpose. `check` is the only request that carries a
card number: it answers with a schedule and a `reference`, and `confirm` sends
that reference alone. So the number crosses the server once, is never stored
(see services/installments/cards.py), and the buyer sees exactly what they are
agreeing to before anything is committed — which is the difference between
"تأكيد الشراء" and signing up for ten months of deductions sight unseen.

The webhook is unauthenticated by necessity — the financier calls it from its
own infrastructure. That is safe here only because it carries no authority: it
is a lookup key and a trigger, and the outcome is then read back from the
financier over an authenticated channel. See services/installments/service.py.
"""
import logging

from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import InstallmentPlan, Location, Reservation
from .serializers import InstallmentPlanSerializer
from .services.installments.base import InstallmentError, NotEligible
from .services.installments.cards import InvalidCardNumber
from .services.installments.registry import get_provider
from .services.installments.service import (
    InstallmentNotAllowed, confirm, options, quote, plan_for_webhook, sync_plan,
)

logger = logging.getLogger(__name__)


class CheckSerializer(serializers.Serializer):
    reservation_id = serializers.IntegerField()
    # The PAN. Never logged, never stored — see cards.py. `trim_whitespace`
    # because people paste it with spaces between the groups.
    card_number = serializers.CharField(max_length=32, trim_whitespace=True)
    # Attached before the quote when the reservation has no address yet (an
    # auction win, a live-stream reserve), because the delivery fee is part of
    # what gets financed and cannot be computed without one.
    delivery_location_id = serializers.IntegerField(required=False)


class ConfirmSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=64)


def _refusal(message, code='not_eligible', http_status=status.HTTP_200_OK):
    """A financier's "no" is an answer, not a failure.

    It comes back 200 with `eligible: false` so the app can show the reason
    next to the card field. Reserving 4xx for faults keeps the two apart in
    the client, in the logs and in the error dashboards.
    """
    return Response({'success': True, 'eligible': False,
                     'code': code, 'message': message}, status=http_status)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def installment_options(request):
    """The terms, or `data: null` when no financier is configured — which is
    how the app knows to leave the installment button out entirely."""
    return Response({'success': True, 'data': options()})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def check_installment(request):
    """Is this card good for this order, and on what schedule?

    Creates a `quoted` plan and returns it. Nothing is committed at the
    financier and no money moves.
    """
    serializer = CheckSerializer(data=request.data)
    if not serializer.is_valid():
        # The errors dict can echo the submitted value back; card_number is
        # stripped from it so a validation message can never carry a PAN.
        errors = dict(serializer.errors)
        errors.pop('card_number', None)
        return Response({'success': False, 'errors': errors},
                        status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    try:
        reservation = Reservation.objects.select_related('item').get(
            pk=data['reservation_id'], buyer=request.user)
    except Reservation.DoesNotExist:
        return Response({'success': False, 'message': 'الحجز غير موجود'},
                        status=status.HTTP_404_NOT_FOUND)

    location_id = data.get('delivery_location_id')
    if location_id and not reservation.delivery_location_id:
        try:
            reservation.delivery_location = Location.objects.get(
                id=location_id, user=request.user)
        except Location.DoesNotExist:
            return Response({'success': False,
                             'message': 'عنوان التسليم غير موجود أو ليس لك'},
                            status=status.HTTP_400_BAD_REQUEST)
        reservation.save(update_fields=['delivery_location'])

    try:
        plan = quote(user=request.user, reservation=reservation,
                     card_number=data['card_number'])
    except InvalidCardNumber as exc:
        return _refusal(str(exc), code='invalid_card')
    except NotEligible as exc:
        return _refusal(exc.reason_ar)
    except InstallmentNotAllowed as exc:
        return _refusal(exc.message_ar, code=exc.code)
    except InstallmentError as exc:
        # A fault, not a refusal: log it with the reservation (never the card)
        # and tell the buyer to try again rather than that they were refused.
        logger.error('installment eligibility failed for reservation %s: %s',
                     reservation.pk, exc)
        return Response({'success': False,
                         'message': 'تعذّر الاتصال بكي كارد — حاول بعد قليل'},
                        status=status.HTTP_502_BAD_GATEWAY)

    return Response({'success': True, 'eligible': True,
                     'data': InstallmentPlanSerializer(plan).data},
                    status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def confirm_installment(request):
    """Commits the quoted plan and completes the reservation in one step.

    This is the installment equivalent of `POST /reservations/{id}/complete/`:
    on success the order is 'completed', the money is in escrow, and the buyer
    owes the financier ten monthly payments.
    """
    serializer = ConfirmSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'success': False, 'errors': serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)

    reference = serializer.validated_data['reference']
    try:
        plan = confirm(user=request.user, reference=reference)
    except InstallmentPlan.DoesNotExist:
        return Response({'success': False, 'message': 'عرض التقسيط غير موجود'},
                        status=status.HTTP_404_NOT_FOUND)
    except NotEligible as exc:
        return _refusal(exc.reason_ar)
    except InstallmentNotAllowed as exc:
        return _refusal(exc.message_ar, code=exc.code)
    except ValueError as exc:
        # complete_reservation's own guard (wrong state, or a charge the
        # financed credit could not cover). The plan was already unwound by
        # service.confirm, so this is a dead end the buyer can retry from.
        logger.error('installment confirm could not complete %s: %s', reference, exc)
        return Response({'success': False, 'code': 'not_completed',
                         'message': 'تعذّر إكمال الحجز — أعد المحاولة'},
                        status=status.HTTP_400_BAD_REQUEST)
    except InstallmentError as exc:
        logger.error('installment confirm failed for %s: %s', reference, exc)
        return Response({'success': False,
                         'message': 'تعذّر إتمام التقسيط — حاول بعد قليل'},
                        status=status.HTTP_502_BAD_GATEWAY)

    _notify_seller(plan)
    return Response({'success': True,
                     'message': 'تم تأكيد الشراء بالتقسيط',
                     'data': InstallmentPlanSerializer(plan).data},
                    status=status.HTTP_200_OK)


def _notify_seller(plan):
    """Same notification the wallet path sends from ReservationViewSet.complete
    — the seller's side of the sale does not care who funded it. Never allowed
    to fail the purchase."""
    from .notifications import notify_reservation_completed
    from .views import send_websocket_notification

    reservation = plan.reservation
    try:
        notify_reservation_completed(reservation)
        send_websocket_notification(
            user_id=reservation.item.owner_id,
            notification_type='reservation_completed',
            title='تم اكمال الحجز',
            message='%s أكمل الحجز لمنتجك "%s" بالتقسيط عبر كي كارد.' % (
                reservation.buyer.username, reservation.item.title),
            data={'reservation_id': reservation.id,
                  'item_title': reservation.item.title},
        )
    except Exception as exc:
        logger.error('failed to notify seller of installment sale %s: %s',
                     plan.reference, exc, exc_info=True)


class InstallmentWebhookView(APIView):
    """Financier callback: a trigger, never a source of truth.

    Whatever the body claims, the only thing read out of it is our own plan
    reference; the plan is then re-fetched from the financier over an
    authenticated channel (service.sync_plan) and the ledger is written from
    that. So a forged callback can, at worst, make the platform ask Qi Card
    about a plan it already knows about.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, provider_name):
        try:
            provider = get_provider(provider_name)
        except InstallmentError:
            return Response({'received': True}, status=status.HTTP_200_OK)

        secret_ok = getattr(provider, 'webhook_secret_ok', lambda value: True)
        if not secret_ok(request.query_params.get('secret')):
            # 200, not 403: an unauthenticated caller learns nothing either
            # way, and a retrying financier should not be handed an error
            # because a secret was rotated on our side.
            logger.warning('installment webhook rejected: bad secret for %s',
                           provider_name)
            return Response({'received': True}, status=status.HTTP_200_OK)

        plan = plan_for_webhook(provider_name, request.data, request.headers)
        if plan is None:
            logger.info('installment webhook for unknown plan (%s)', provider_name)
            return Response({'received': True}, status=status.HTTP_200_OK)

        try:
            sync_plan(plan)
        except InstallmentError as exc:
            # The financier will retry; answering 200 anyway would tell it the
            # event was handled when the authoritative read never happened.
            logger.error('installment webhook sync failed for %s: %s',
                         plan.reference, exc)
            return Response({'received': False},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({'received': True}, status=status.HTTP_200_OK)
