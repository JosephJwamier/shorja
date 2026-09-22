from datetime import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import ItemVariant
from .models import Contact, LedgerEntry
from .serializers import (
    ContactSerializer, DashboardSerializer, LedgerEntrySerializer,
    StatementRowSerializer, SummarySerializer, VoidSerializer,
)
from .services import dashboard, open_balances, period_bounds, summarize


def store_of(request):
    """The book belongs to the store, and only its owner may open it."""
    store = getattr(request.user, 'store', None)
    if store is None:
        raise PermissionDenied('أنشئ متجرك أولاً لاستخدام دفتر الحساب')
    return store


class StoreScopedMixin:
    permission_classes = [IsAuthenticated]

    @property
    def store(self):
        if not hasattr(self, '_store'):
            self._store = store_of(self.request)
        return self._store

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['store'] = self.store
        return ctx


class EntryPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = 'page_size'
    max_page_size = 200


class ContactViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    serializer_class = ContactSerializer
    # A shop's contact book is small and the app wants it whole (picker,
    # balances); entries are paginated instead.
    pagination_class = None
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = Contact.objects.filter(store=self.store).select_related('linked_user')
        params = self.request.query_params
        kind = params.get('kind')
        if kind in (Contact.KIND_CUSTOMER, Contact.KIND_SUPPLIER):
            qs = qs.filter(kind=kind)
        if params.get('archived') != '1':
            qs = qs.filter(is_archived=False)
        q = params.get('q')
        if q:
            qs = qs.filter(name__icontains=q) | qs.filter(phone__icontains=q)
        qs = Contact.annotate_balance(qs)
        if params.get('ordering') == 'balance':
            qs = qs.order_by('-balance', 'name')
        return qs

    def perform_create(self, serializer):
        serializer.save(store=self.store)

    def retrieve(self, request, *args, **kwargs):
        # Balance is annotated in the list queryset; make sure a single
        # fetch carries it too.
        instance = self.get_object()
        return Response(self.get_serializer(instance).data)

    def destroy(self, request, *args, **kwargs):
        """Never a real delete once there's history."""
        contact = self.get_object()
        if contact.entries.exists():
            contact.is_archived = True
            contact.save(update_fields=['is_archived', 'updated_at'])
            return Response({'archived': True}, status=status.HTTP_200_OK)
        contact.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def unarchive(self, request, pk=None):
        contact = self.get_object()
        contact.is_archived = False
        contact.save(update_fields=['is_archived', 'updated_at'])
        return Response(self.get_serializer(contact).data)

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """كشف حساب: every entry on this contact, oldest first, with the
        running balance after each line — what the merchant shares with
        the customer."""
        contact = self.get_object()
        entries = (
            contact.entries.filter(is_void=False)
            .select_related('item', 'variant')
            .order_by('occurred_at', 'id')
        )
        running = 0
        rows = []
        for e in entries:
            if e.kind in Contact.BALANCE_UP:
                running += e.amount
            elif e.kind in Contact.BALANCE_DOWN:
                running -= e.amount
            rows.append({'entry': e, 'running_balance': running})
        return Response({
            'contact': self.get_serializer(contact).data,
            'balance': str(running),
            'rows': StatementRowSerializer(rows, many=True, context=self.get_serializer_context()).data,
        })


class LedgerEntryViewSet(StoreScopedMixin, viewsets.ModelViewSet):
    serializer_class = LedgerEntrySerializer
    pagination_class = EntryPagination
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']  # no delete: void instead

    def get_queryset(self):
        qs = (
            LedgerEntry.objects.filter(store=self.store)
            .select_related('contact', 'item', 'variant')
        )
        params = self.request.query_params
        kind = params.get('kind')
        if kind:
            kinds = [k for k in kind.split(',') if k in dict(LedgerEntry.KIND_CHOICES)]
            qs = qs.filter(kind__in=kinds)
        if params.get('contact'):
            qs = qs.filter(contact_id=params['contact'])
        if params.get('source') in (LedgerEntry.SOURCE_MANUAL, LedgerEntry.SOURCE_APP):
            qs = qs.filter(source=params['source'])
        if params.get('include_void') != '1':
            qs = qs.filter(is_void=False)
        start = parse_date(params.get('start') or '')
        end = parse_date(params.get('end') or '')
        if start:
            qs = qs.filter(occurred_at__date__gte=start)
        if end:
            qs = qs.filter(occurred_at__date__lte=end)
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        variant = data.get('variant')
        cost = None
        if variant is not None and data.get('quantity'):
            # Lock and take stock through the one sanctioned path.
            variant = ItemVariant.objects.select_for_update().get(pk=variant.pk)
            try:
                variant.take_stock(data['quantity'])
            except ValueError:
                raise ValidationError({'quantity': f'المتوفّر {variant.quantity} فقط'})
            if variant.cost_price is not None:
                cost = variant.cost_price * data['quantity']
        serializer.save(store=self.store, created_by=self.request.user, cost_amount=cost)

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        entry = self.get_object()
        if entry.source == LedgerEntry.SOURCE_APP:
            return Response({'detail': 'قيود التطبيق تُلغى من الحجز نفسه'},
                            status=status.HTTP_400_BAD_REQUEST)
        ser = VoidSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            entry.void(ser.validated_data['reason'])
        return Response(self.get_serializer(entry).data)


def _period_from(request):
    params = request.query_params
    period = params.get('period', 'month')
    start = parse_date(params.get('start') or '')
    end = parse_date(params.get('end') or '')
    tz = timezone.get_current_timezone()
    if period == 'custom':
        if not (start and end) or start > end:
            raise ValidationError({'period': 'حدّد تاريخ البداية والنهاية'})
        start = timezone.make_aware(datetime.combine(start, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(end, datetime.min.time()), tz)
    return period, start, end


class SummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        store = store_of(request)
        period, start, end = _period_from(request)
        s, e, _, _ = period_bounds(period, start, end)
        data = SummarySerializer(summarize(store, s, e)).data
        balances = open_balances(store)
        data['receivables'] = str(balances['receivables'])
        data['payables'] = str(balances['payables'])
        return Response(data)


class DashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        store = store_of(request)
        period, start, end = _period_from(request)
        return Response(DashboardSerializer(dashboard(store, period, start, end)).data)
