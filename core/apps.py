from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        from .admin_site import configure_admin_site
        configure_admin_site()

        # Import for side effects: connects the model invariants in signals.py.
        from . import signals  # noqa: F401

        self._register_auditlog_models()

    def _register_auditlog_models(self):
        """Track who changed what, and when, on the models where that
        matters most: money movement, dispute outcomes, and account access."""
        from auditlog.registry import auditlog
        from .models import Dispute, Transaction, Wallet, WithdrawalRequest, DepositRequest, User

        auditlog.register(Dispute)
        auditlog.register(Transaction)
        auditlog.register(Wallet)
        auditlog.register(WithdrawalRequest)
        auditlog.register(DepositRequest)
        auditlog.register(User, exclude_fields=[
            'password', 'verification_code', 'verification_code_sent_at', 'fcm_token',
        ])
