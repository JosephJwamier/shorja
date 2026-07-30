"""
core/legal_views.py
صفحات HTML علنية للوثائق القانونية — مطلوبة كرابط علني لمتاجر التطبيقات
(Google Play / App Store) وللإعلانات المدفوعة:
    /legal/privacy/?lang=ar|en
    /legal/terms/?lang=ar|en
"""
from django.http import Http404
from django.shortcuts import render

from .models import LegalDocument


def legal_document_page(request, doc_type):
    if doc_type not in (LegalDocument.DOC_PRIVACY, LegalDocument.DOC_TERMS):
        raise Http404('Unknown document type')

    lang = request.GET.get('lang', LegalDocument.LANG_AR)
    doc = (
        LegalDocument.current(doc_type, lang)
        or LegalDocument.current(doc_type, LegalDocument.LANG_AR)
        or LegalDocument.current(doc_type, LegalDocument.LANG_EN)
    )
    if not doc:
        raise Http404('Document not available')

    other_lang = (
        LegalDocument.LANG_EN if doc.language == LegalDocument.LANG_AR
        else LegalDocument.LANG_AR
    )
    has_other = LegalDocument.current(doc_type, other_lang) is not None

    return render(request, 'legal/document.html', {
        'doc': doc,
        'is_rtl': doc.language == LegalDocument.LANG_AR,
        'doc_type': doc_type,
        'other_lang': other_lang,
        'has_other': has_other,
    })
