"""
core/legal_api.py
واجهة REST للنظام القانوني:
  GET  /api/legal/documents/                 قائمة الوثائق المنشورة (?type=&lang=)
  GET  /api/legal/documents/current/         أحدث إصدار (?type=privacy|terms&lang=ar|en)
  GET  /api/legal/documents/pending/         (مصادق) الوثائق التي تنتظر موافقة هذا المستخدم
  POST /api/legal/documents/accept/          (مصادق) تسجيل موافقة المستخدم
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import LegalDocument, LegalAcceptance
from .legal_serializers import (
    LegalDocumentSerializer,
    LegalDocumentListSerializer,
)

_DOC_TYPES = (LegalDocument.DOC_PRIVACY, LegalDocument.DOC_TERMS)


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _current_doc(doc_type, language):
    """أحدث إصدار منشور للغة المطلوبة، مع الرجوع للعربية إن لم تتوفّر."""
    return (
        LegalDocument.current(doc_type, language)
        or LegalDocument.current(doc_type, LegalDocument.LANG_AR)
    )


class LegalDocumentViewSet(viewsets.ReadOnlyModelViewSet):
    """عرض الوثائق القانونية المنشورة وتسجيل موافقة المستخدم عليها."""
    serializer_class = LegalDocumentSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        qs = LegalDocument.objects.filter(is_published=True)
        doc_type = self.request.query_params.get('type')
        language = self.request.query_params.get('lang')
        if doc_type:
            qs = qs.filter(doc_type=doc_type)
        if language:
            qs = qs.filter(language=language)
        return qs.order_by('doc_type', 'language', '-version')

    @action(detail=False, methods=['get'])
    def current(self, request):
        """أحدث إصدار منشور. المعاملات: ?type=privacy|terms&lang=ar|en"""
        doc_type = request.query_params.get('type')
        language = request.query_params.get('lang', LegalDocument.LANG_AR)
        if doc_type not in _DOC_TYPES:
            return Response(
                {'detail': "المعامل type يجب أن يكون privacy أو terms."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        doc = _current_doc(doc_type, language)
        if not doc:
            return Response({'detail': 'لا توجد وثيقة منشورة.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(self.get_serializer(doc).data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def pending(self, request):
        """
        الوثائق الحالية التي لم يوافق المستخدم على إصدارها الحالي بعد.
        الموافقة تُحتسب على مستوى (النوع + الإصدار) بغضّ النظر عن اللغة.
        """
        language = request.query_params.get('lang', LegalDocument.LANG_AR)
        accepted = (
            LegalAcceptance.objects
            .filter(user=request.user)
            .select_related('document')
        )
        accepted_type_versions = {
            (a.document.doc_type, a.document.version) for a in accepted
        }
        pending_docs = []
        for doc_type in _DOC_TYPES:
            doc = _current_doc(doc_type, language)
            if (
                doc
                and doc.requires_reacceptance
                and (doc.doc_type, doc.version) not in accepted_type_versions
            ):
                pending_docs.append(doc)
        ser = LegalDocumentListSerializer(
            pending_docs, many=True, context={'request': request})
        return Response({'must_accept': bool(pending_docs), 'pending': ser.data})

    @action(detail=False, methods=['post'], permission_classes=[IsAuthenticated])
    def accept(self, request):
        """
        تسجيل موافقة المستخدم.
        body:
          {"ids": [1,2]}                    وثائق محدّدة، أو
          {"lang": "ar"} / {}               = كل الوثائق الحالية للّغة.
          {"app_version": "1.0.0+1"}        (اختياري) للتوثيق.
        """
        ids = request.data.get('ids')
        language = request.data.get('lang', LegalDocument.LANG_AR)
        if ids:
            docs = list(LegalDocument.objects.filter(id__in=ids, is_published=True))
        else:
            docs = [d for d in (_current_doc(t, language) for t in _DOC_TYPES) if d]

        if not docs:
            return Response({'detail': 'لا توجد وثائق لتسجيل الموافقة عليها.'},
                            status=status.HTTP_400_BAD_REQUEST)

        ip = _client_ip(request)
        app_version = str(request.data.get('app_version') or '')[:40]
        user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:300]

        newly = 0
        for doc in docs:
            _, created = LegalAcceptance.objects.get_or_create(
                user=request.user, document=doc,
                defaults={
                    'ip_address': ip,
                    'app_version': app_version,
                    'user_agent': user_agent,
                },
            )
            newly += int(created)

        return Response(
            {'accepted': len(docs), 'newly_recorded': newly},
            status=status.HTTP_201_CREATED,
        )
