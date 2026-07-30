"""
core/legal_serializers.py
مُسلسِلات النظام القانوني (سياسة الخصوصية / شروط الاستخدام + الموافقات).
مفصولة عن serializers.py الرئيسي لعزل الميزة وتسهيل صيانتها.
"""
from rest_framework import serializers

from .models import LegalDocument, LegalAcceptance


def _public_url(obj, context):
    request = context.get('request')
    rel = f"/legal/{obj.doc_type}/?lang={obj.language}"
    return request.build_absolute_uri(rel) if request else rel


class LegalDocumentSerializer(serializers.ModelSerializer):
    """النسخة الكاملة — تتضمّن محتوى HTML."""
    doc_type_display = serializers.CharField(source='get_doc_type_display', read_only=True)
    public_url = serializers.SerializerMethodField()

    class Meta:
        model = LegalDocument
        fields = [
            'id', 'doc_type', 'doc_type_display', 'language', 'version',
            'title', 'content', 'summary', 'effective_at', 'is_published',
            'public_url', 'created_at', 'updated_at',
        ]

    def get_public_url(self, obj):
        return _public_url(obj, self.context)


class LegalDocumentListSerializer(serializers.ModelSerializer):
    """نسخة خفيفة بلا المحتوى الكامل — للقوائم وحالة الموافقة."""
    doc_type_display = serializers.CharField(source='get_doc_type_display', read_only=True)
    public_url = serializers.SerializerMethodField()

    class Meta:
        model = LegalDocument
        fields = [
            'id', 'doc_type', 'doc_type_display', 'language', 'version',
            'title', 'summary', 'effective_at', 'public_url',
        ]

    def get_public_url(self, obj):
        return _public_url(obj, self.context)


class LegalAcceptanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalAcceptance
        fields = ['id', 'document', 'accepted_at', 'app_version']
        read_only_fields = fields
