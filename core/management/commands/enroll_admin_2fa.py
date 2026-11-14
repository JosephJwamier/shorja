"""Bind an authenticator app to a staff account — from the server only.

With settings.ADMIN_REQUIRE_2FA on, the admin requires an OTP-verified
session; enrol here first, then switch it on. The web offers no way to enrol
(see core/admin_site.py): there the password is the only proof of identity,
so whoever knew it first could bind their own phone. Running this command
needs a shell on the server, which the password alone does not give.

Once a device is enrolled, the sign-in page asks for its code even while
ADMIN_REQUIRE_2FA is off — two_factor's login does that for any account with
a confirmed 'default' device.

    docker compose -f deploy/docker-compose.yml --env-file .env \\
        exec web python manage.py enroll_admin_2fa <username>

Scan the QR code (or type the key) in Google Authenticator, Microsoft
Authenticator or similar, then type the 6-digit code the app shows. Nothing is
saved until that code checks out. Ten single-use backup codes are printed at
the end; each one replaces a TOTP code once, for a lost phone.

--replace removes the account's existing devices first (a new phone).
"""
import io
import time
from base64 import b32encode

import qrcode
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp import devices_for_user
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

BACKUP_CODES = 10
ATTEMPTS = 3


class Command(BaseCommand):
    help = 'Bind an authenticator app to a staff account (admin two-factor).'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument(
            '--replace', action='store_true',
            help="Remove the account's existing devices first (new phone).")

    def handle(self, *args, username, replace, **options):
        user = self._staff_user(username)
        if not replace and any(devices_for_user(user, confirmed=True)):
            raise CommandError(
                f'"{username}" already has a confirmed device. Use --replace '
                'to start over with a new phone.')

        # 'default' is load-bearing: two_factor's login only asks for a code
        # when the account has a confirmed device of that name.
        device = TOTPDevice(user=user, name='default', confirmed=False)
        self._show(device)
        totp = self._read_matching_code(device)

        with transaction.atomic():
            # Every other device goes: the old phone (with --replace), and any
            # half-finished enrolment left by the old web wizard.
            for old in devices_for_user(user, confirmed=None):
                old.delete()
            device.confirmed = True
            device.last_t = totp.t()  # the code just typed cannot be replayed
            device.save()
            codes = self._backup_codes(user)

        self.stdout.write(self.style.SUCCESS(
            f'\nTwo-factor is on for "{username}". Sign in to the admin again '
            'and enter the code from the app.'))
        self.stdout.write(
            '\nBackup codes — each works once in place of a code, for a lost '
            'phone. Keep them somewhere safe, off this server:\n')
        for code in codes:
            self.stdout.write(f'    {code}')

    def _staff_user(self, username):
        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            raise CommandError(f'No user named "{username}".')
        if not user.is_staff:
            raise CommandError(
                f'"{username}" is not staff; two-factor guards the admin only.')
        return user

    def _show(self, device):
        qr = qrcode.QRCode(border=2)
        qr.add_data(device.config_url)
        qr.make(fit=True)
        art = io.StringIO()
        qr.print_ascii(out=art, invert=True)
        self.stdout.write(art.getvalue())
        self.stdout.write(
            'Scan this in your authenticator app, or add it by hand with '
            'this key:\n')
        self.stdout.write(f'    {b32encode(device.bin_key).decode()}\n')

    def _read_matching_code(self, device):
        for attempt in range(1, ATTEMPTS + 1):
            code = input(f'6-digit code from the app ({attempt}/{ATTEMPTS}): ').strip()
            totp = TOTP(device.bin_key, device.step, device.t0, device.digits)
            totp.time = time.time()
            if code.isdigit() and totp.verify(int(code), tolerance=1):
                return totp
            self.stdout.write(self.style.WARNING(
                'That code does not match. Check the phone clock is automatic.'))
        raise CommandError('No matching code; nothing was saved. Run the command again.')

    def _backup_codes(self, user):
        static = StaticDevice.objects.create(user=user, name='backup', confirmed=True)
        codes = [StaticToken.random_token() for _ in range(BACKUP_CODES)]
        StaticToken.objects.bulk_create(
            StaticToken(device=static, token=code) for code in codes)
        return codes
