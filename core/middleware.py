from django.conf import settings
from django.utils import translation


class AdminLocaleMiddleware:
    """
    Activates Arabic for the admin panel and its login/2FA pages only, so
    django-unfold renders dir="rtl" and Django's own admin chrome strings
    ("Add", "Delete", "Log out", ...) come out in Arabic. Left off the API
    on purpose — DRF error strings and any other Django-generated text the
    Flutter app parses need to stay on LANGUAGE_CODE (English).
    """

    ADMIN_ADJACENT_PREFIXES = ('/account/',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        admin_prefix = f'/{settings.ADMIN_URL_PATH}'
        is_admin_request = request.path.startswith(admin_prefix) or request.path.startswith(
            self.ADMIN_ADJACENT_PREFIXES
        )

        if is_admin_request:
            translation.activate('ar')
        else:
            translation.activate(settings.LANGUAGE_CODE)

        try:
            response = self.get_response(request)
        finally:
            translation.deactivate()

        return response
