"""
ReSell Trust - Authentication Views (core/auth_views.py)
UPDATED: Added default location creation during registration
"""

from rest_framework import status, generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from rest_framework.throttling import ScopedRateThrottle
from .throttling import LoginUsernameThrottle, PasswordResetEmailThrottle
from django.contrib.auth import authenticate, get_user_model
from django.db import transaction  # NEW
from django.utils import timezone
import logging
import re

from .serializers import (
    UserRegistrationSerializer,
    UserLoginSerializer,
    UserSerializer,
    ChangePasswordSerializer,
    ResetPasswordSerializer,
    LogoutSerializer,
)
from .notifications import NotificationService
from .phone_utils import to_e164
from .models import Location  # NEW: Import Location

User = get_user_model()
logger = logging.getLogger(__name__)


class RegisterView(generics.CreateAPIView):
    """User registration endpoint"""
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_register'

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # NEW: Use transaction to ensure all objects are created together
        with transaction.atomic():
            user = serializer.save()

            # Create default location for user (if address provided during registration)
            default_location = None
            if request.data.get('address_line1'):
                default_location = Location.objects.create(
                    user=user,
                    location_type='user_address',
                    name=f"{user.username}'s Primary Address",
                    address_line1=request.data.get('address_line1', ''),
                    address_line2=request.data.get('address_line2', ''),
                    city=request.data.get('city', ''),
                    state=request.data.get('state', ''),
                    postal_code=request.data.get('postal_code', ''),
                    country=request.data.get('country', 'Iraq'),  # Default country
                    contact_person=user.get_full_name() or user.username,
                    contact_phone=user.phone or '',
                    is_primary=True,
                    is_active=True
                )

        # Only when the user gave one: email is optional now, and
        # send_email_verification_code() on a NULL address would raise.
        if user.email:
            send_email_verification_code(user)

        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)

        return Response({
            'user': UserSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'message': (
                'Registration successful. Please verify your email.'
                if user.email else
                'Registration successful. Please verify your phone number.'
            ),
            'has_default_location': default_location is not None,  # NEW
            'location_id': default_location.id if default_location else None  # NEW
        }, status=status.HTTP_201_CREATED)


class LoginView(generics.GenericAPIView):
    """User login endpoint"""
    permission_classes = [AllowAny]
    serializer_class = UserLoginSerializer
    # Per-IP (scope) AND per-username: see core/throttling.py for why both.
    throttle_classes = [ScopedRateThrottle, LoginUsernameThrottle]
    throttle_scope = 'auth_login'

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = (serializer.validated_data['username'] or '').strip()
        password = serializer.validated_data['password']

        # Resolve whatever was typed down to a username. A real username
        # always wins, so adding these fallbacks cannot change the meaning
        # of an existing login; only unmatched input is tried as an email
        # or a phone.
        #
        # (The previous version left `username` unbound when an '@' string
        # matched no account, raising UnboundLocalError — a 500 on what is
        # simply a wrong address.)
        username = identifier
        if not User.objects.filter(username=identifier).exists():
            if '@' in identifier:
                match = User.objects.filter(email=identifier.lower()).first()
            else:
                # Any spelling of the number reaches the one stored form.
                canonical = to_e164(identifier)
                match = User.objects.filter(phone=canonical).first() if canonical else None
            if match:
                username = match.username

        user = authenticate(request, username=username, password=password)

        if user is None:
            return Response({
                'error': 'Invalid credentials'
            }, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response({
                'error': 'Account is disabled'
            }, status=status.HTTP_403_FORBIDDEN)

        # Generate tokens
        refresh = RefreshToken.for_user(user)

        # Baseline "last seen" for a user who never opens a chat — the live
        # updates during a session come from ChatConsumer instead.
        User.objects.filter(pk=user.pk).update(last_seen_at=timezone.now())

        # NEW: Check if user has locations
        has_locations = Location.objects.filter(user=user).exists()
        primary_location = Location.objects.filter(user=user, is_primary=True).first()

        return Response({
            'user': UserSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'has_locations': has_locations,  # NEW
            'primary_location_id': primary_location.id if primary_location else None,  # NEW
            'needs_location_setup': not has_locations  # NEW: Flag to show location setup wizard
        })


class LogoutView(generics.GenericAPIView):
    """User logout endpoint"""
    permission_classes = [IsAuthenticated]
    serializer_class = LogoutSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            refresh_token = serializer.validated_data['refresh_token']
            token = RefreshToken(refresh_token)
            token.blacklist()

            return Response(
                {'message': 'Logout successful'},
                status=status.HTTP_205_RESET_CONTENT
            )

        except TokenError:
            return Response(
                {'error': 'Invalid or expired token'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {'error': 'Logout failed'},
                status=status.HTTP_400_BAD_REQUEST
            )


def send_email_verification_code(user):
    """Generate a 6-digit code and email it to the user (used by register + resend).
    Returns whether the email actually went out."""
    code = user.generate_verification_code('email')
    message = f"""
    مرحباً {user.username}،

    رمز تفعيل بريدك الإلكتروني في سوقي هو: {code}

    ينتهي صلاحية هذا الرمز خلال 15 دقيقة.

    فريق سوقي
    """
    return NotificationService.send_email(
        to_email=user.email,
        subject="تفعيل بريدك الإلكتروني - سوقي",
        message=message
    )


class VerifyEmailView(generics.GenericAPIView):
    """Email verification endpoint — checks a 6-digit code sent to the user's email"""
    permission_classes = [IsAuthenticated]
    swagger_schema = None

    def post(self, request):
        user = request.user
        code = request.data.get('code', '')

        if user.email_verified:
            return Response({'message': 'Email already verified'})

        if user.check_verification_code('email', code):
            user.email_verified = True
            user.save(update_fields=['email_verified'])
            user.clear_verification_code()
            return Response({'message': 'Email verified successfully'})

        return Response({
            'error': 'Invalid or expired code'
        }, status=status.HTTP_400_BAD_REQUEST)


def normalize_phone_suffix(value):
    """The comparable part of an Iraqi mobile number: its last 9 digits.

    Firebase always hands back E.164 ('+9647701234567') while User.phone is
    whatever the user typed — usually local ('07701234567'), sometimes already
    international. The national significant number without the leading zero is
    the part both forms share, so comparing on it matches them without having
    to guess which format a given row happens to hold.

    Returns '' for anything too short to be a real number, which never
    compares equal (the caller must reject empties rather than treat two
    blanks as a match).
    """
    digits = re.sub(r'\D', '', value or '')
    return digits[-9:] if len(digits) >= 9 else ''


class VerifyPhoneView(APIView):
    """Phone verification.

    The only accepted proof is `id_token` — a Firebase ID token carrying a
    verified `phone_number` claim. Firebase Phone Auth generates and delivers
    the SMS itself, from the device, so there is no server-side code to
    compare against; our job is to verify the token and confirm the phone in
    it belongs to this account. Same verification path as SocialLoginView.

    (The pre-launch 6-digit code sent over Twilio was removed before any
    build using it shipped — there is no older client to stay compatible
    with.)
    """
    permission_classes = [IsAuthenticated]
    swagger_schema = None

    def post(self, request):
        user = request.user

        if user.phone_verified:
            return Response({'message': 'Phone already verified'})

        id_token = request.data.get('id_token')
        if not id_token:
            return Response({
                'error': 'id_token is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        return self._verify_with_firebase(user, id_token)

    def _verify_with_firebase(self, user, id_token):
        from .firebase import get_firebase_app, FIREBASE_AVAILABLE

        if not FIREBASE_AVAILABLE or get_firebase_app() is None:
            return Response({
                'error': 'Firebase is not configured on the server'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        from firebase_admin import auth as firebase_auth

        try:
            decoded = firebase_auth.verify_id_token(id_token)
        except Exception as e:
            logger.warning(f"Phone verification token check failed: {e}")
            return Response({
                'error': 'Invalid or expired token'
            }, status=status.HTTP_401_UNAUTHORIZED)

        token_phone = normalize_phone_suffix(decoded.get('phone_number'))
        if not token_phone:
            return Response({
                'error': 'This sign-in carries no verified phone number'
            }, status=status.HTTP_400_BAD_REQUEST)

        account_phone = normalize_phone_suffix(user.phone)
        if not account_phone:
            return Response({
                'error': 'Phone number not set'
            }, status=status.HTTP_400_BAD_REQUEST)

        # The token proves someone owns *that* phone — not that it's the phone
        # on this account. Without this check any verified number would mark
        # any account's number verified.
        if token_phone != account_phone:
            return Response({
                'error': 'The verified number does not match this account'
            }, status=status.HTTP_400_BAD_REQUEST)

        # One phone, one account — now enforced by the UNIQUE index on
        # User.phone rather than checked here. The old 409 branch scanned for
        # another account holding this number; with the constraint in place
        # no such row can exist, so it was unreachable and has been removed.
        # If uniqueness is ever relaxed, this is where the check belongs.

        user.phone_verified = True
        user.save(update_fields=['phone_verified'])
        user.clear_verification_code()
        # firebase_uid is deliberately left alone: it identifies the user's
        # Google/Facebook identity for SocialLoginView, and overwriting it with
        # a phone-only Firebase UID would break that lookup (and collide on its
        # unique constraint).
        return Response({'message': 'Phone verified successfully'})


class ResendVerificationView(APIView):
    """Resend email verification"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        if user.email_verified:
            return Response({
                'message': 'Email already verified'
            }, status=status.HTTP_400_BAD_REQUEST)

        sent = send_email_verification_code(user)
        if not sent:
            return Response({
                'error': 'Failed to send verification email — try again later'
            }, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'message': 'Verification email sent'})


class ChangePasswordView(APIView):
    """Change password for authenticated user"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        old_password = serializer.validated_data['old_password']
        new_password = serializer.validated_data['new_password']

        if not user.check_password(old_password):
            return Response({
                'error': 'Old password is incorrect'
            }, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()

        return Response({'message': 'Password changed successfully'})


class ResetPasswordView(APIView):
    """Request password reset — emails a 6-digit code (no web link needed)"""
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, PasswordResetEmailThrottle]
    throttle_scope = 'auth_reset'

    def post(self, request):
        # Normalised and required: email is NULL for most accounts now, and
        # a blank request must not resolve to one of them. (`.get(email='')`
        # on the old non-unique column would have matched every phone-only
        # account at once and raised MultipleObjectsReturned — a 500 on an
        # empty form field.)
        email = (request.data.get('email') or '').strip().lower()

        try:
            if not email:
                raise User.DoesNotExist
            user = User.objects.get(email=email)
            code = user.generate_verification_code('password_reset')

            message = f"""
            مرحباً {user.username}،

            لقد طلبت إعادة تعيين كلمة المرور. رمزك هو: {code}

            ينتهي صلاحية هذا الرمز خلال 15 دقيقة. إذا لم تطلب ذلك، يرجى تجاهل هذه الرسالة.

            فريق سوقي
            """

            NotificationService.send_email(
                to_email=user.email,
                subject="إعادة تعيين كلمة المرور - سوقي",
                message=message
            )

        except User.DoesNotExist:
            # Don't reveal if user exists
            pass

        return Response({
            'message': 'If your email is registered, you will receive a password reset code'
        })


class ResetPasswordConfirmView(APIView):
    """Confirm password reset with the emailed code"""
    permission_classes = [AllowAny]
    # Same per-email bucket as the request step: 6-digit code, so the
    # confirm endpoint is the one an attacker would actually brute-force.
    throttle_classes = [ScopedRateThrottle, PasswordResetEmailThrottle]
    throttle_scope = 'auth_reset'

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = (serializer.validated_data['email'] or '').strip().lower()
        code = serializer.validated_data['code']

        try:
            if not email:
                raise User.DoesNotExist
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({
                'error': 'Invalid or expired code'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_verification_code('password_reset', code):
            return Response({
                'error': 'Invalid or expired code'
            }, status=status.HTTP_400_BAD_REQUEST)

        new_password = serializer.validated_data['new_password']
        user.set_password(new_password)
        user.save()
        user.clear_verification_code()

        return Response({'message': 'Password reset successful'})


class ResetPasswordByPhoneView(APIView):
    """Reset a password with proof of owning the account's phone.

    Email is optional now, so most accounts have no address to send a code
    to — the phone is what every account has. There is no code to generate
    or compare here: Firebase Phone Auth sends the SMS from the device and
    mints an ID token carrying a verified `phone_number` claim. Possession
    of that token *is* the proof, verified the same way VerifyPhoneView and
    SocialLoginView verify theirs.

    Deliberately not revealing whether a number is registered: the response
    is identical whether the phone matched an account or not, so this cannot
    be used to enumerate which numbers hold accounts.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_reset'

    GENERIC_OK = {'message': 'If that number has an account, its password has been reset'}

    def post(self, request):
        from .firebase import get_firebase_app, FIREBASE_AVAILABLE

        id_token = request.data.get('id_token')
        new_password = request.data.get('new_password') or ''

        if not id_token:
            return Response({'error': 'id_token is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 8:
            return Response({'error': 'كلمة المرور يجب أن تكون 8 أحرف على الأقل'},
                            status=status.HTTP_400_BAD_REQUEST)

        if not FIREBASE_AVAILABLE or get_firebase_app() is None:
            return Response({'error': 'Firebase is not configured on the server'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        from firebase_admin import auth as firebase_auth
        try:
            decoded = firebase_auth.verify_id_token(id_token)
        except Exception as e:
            logger.warning(f"Phone password reset token check failed: {e}")
            return Response({'error': 'Invalid or expired token'},
                            status=status.HTTP_401_UNAUTHORIZED)

        # Only a phone sign-in carries this claim; a Google token does not,
        # so a Google session cannot be replayed here to seize an account.
        canonical = to_e164(decoded.get('phone_number'))
        if not canonical:
            return Response({'error': 'This sign-in carries no verified phone number'},
                            status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(phone=canonical).first()
        if user is None:
            return Response(self.GENERIC_OK)

        user.set_password(new_password)
        # The SMS proved the number reaches them; recording that saves the
        # user a second verification immediately after recovering.
        user.phone_verified = True
        user.save(update_fields=['password', 'phone_verified'])
        user.clear_verification_code()
        logger.info(f"Password reset by phone for user {user.id}")

        return Response(self.GENERIC_OK)


class UserProfileView(generics.RetrieveAPIView):
    """Get current user profile"""
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class UpdateProfileView(generics.UpdateAPIView):
    """Update user profile"""
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


def verify_firebase_token(id_token):
    """(decoded, error_response). Shared by every endpoint that trusts a
    Firebase ID token, so they all fail the same way."""
    from .firebase import get_firebase_app, FIREBASE_AVAILABLE

    if not FIREBASE_AVAILABLE or get_firebase_app() is None:
        return None, Response({'error': 'Firebase is not configured on the server'},
                              status=status.HTTP_503_SERVICE_UNAVAILABLE)
    from firebase_admin import auth as firebase_auth
    try:
        return firebase_auth.verify_id_token(id_token), None
    except Exception as e:
        logger.warning(f"Firebase token verification failed: {e}")
        return None, Response({'error': 'Invalid or expired token'},
                              status=status.HTTP_401_UNAUTHORIZED)


def resolve_social_user(uid, email):
    """The account behind a Google identity, or None.

    Matches on firebase_uid first, then on the email — the second case is
    someone who registered with a password and later signed in with Google
    on the same address; linking the uid keeps it one account rather than
    silently starting a second.
    """
    user = User.objects.filter(firebase_uid=uid).first()
    if user is not None:
        return user

    email = (email or '').strip().lower()
    if not email:
        return None
    user = User.objects.filter(email=email).first()
    if user is not None:
        user.firebase_uid = uid
        user.save(update_fields=['firebase_uid'])
    return user


def unique_username(email, name):
    """A username derived from the email local-part, falling back to the
    display name, with a numeric suffix on collision."""
    base = (email.split('@')[0] if email else name) or 'user'
    base = re.sub(r'[^a-zA-Z0-9_]', '', base).lower() or 'user'
    base = base[:25]

    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base}{suffix}"
    return username


def issue_session(user, is_new_user):
    """The one success shape for every sign-in route, so the app has a
    single branch to handle rather than one per provider."""
    refresh = RefreshToken.for_user(user)
    has_locations = Location.objects.filter(user=user).exists()
    primary_location = Location.objects.filter(user=user, is_primary=True).first()

    return Response({
        'user': UserSerializer(user).data,
        'tokens': {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        },
        'is_new_user': is_new_user,
        'has_locations': has_locations,
        'primary_location_id': primary_location.id if primary_location else None,
        'needs_location_setup': not has_locations,
    })


class SocialLoginView(APIView):
    """
    Sign in (or register) via Google/Facebook. The client signs in to Firebase
    Auth with the native Google/Facebook credential and sends us Firebase's own
    ID token — we verify it with firebase_admin (already configured for the
    firebase-token bridge above) instead of talking to Google/Facebook directly.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_login'

    def post(self, request):
        id_token = request.data.get('id_token')
        if not id_token:
            return Response({'error': 'id_token is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        decoded, error = verify_firebase_token(id_token)
        if error:
            return error

        uid = decoded['uid']
        name = decoded.get('name', '')

        user = resolve_social_user(uid, decoded.get('email'))

        # Every account must carry a verified phone, so this endpoint can
        # only finish for someone who already has one. Google will not tell
        # us the number — a Google ID token has no phone_number claim, and
        # reading it from the People API needs a scope the user must have
        # set up and granted, which usually comes back empty. So the client
        # collects it instead: enter the number, receive Firebase's SMS, and
        # come back to SocialAttachPhoneView with both tokens.
        #
        # Nothing is written here. Creating the account now and chasing the
        # phone afterwards would leave unreachable rows behind every
        # abandoned signup — accounts whose owner can never recover them.
        if user is None or not user.phone_verified:
            return Response({
                'status': 'phone_required',
                'detail': 'يلزم توثيق رقم الهاتف لإكمال تسجيل الدخول',
                'email': decoded.get('email') or '',
                'name': name,
                'is_new_user': user is None,
            })

        return issue_session(user, is_new_user=False)



class SocialAttachPhoneView(APIView):
    """Second half of a Google sign-in: attach a verified phone, then finish.

    SocialLoginView answers `phone_required` and writes nothing. The client
    collects the number, lets Firebase Phone Auth send and check the SMS,
    and posts both tokens here:

      id_token        the Google token from the first step — still the proof
                      of *which* Google identity is signing in
      phone_id_token  a phone token, proof that this device holds the number

    Both are required. The Google token alone would let anyone attach any
    number; the phone token alone would not say whose account to attach it
    to. The account is created here, not earlier, so an abandoned signup
    leaves nothing behind.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_login'

    def post(self, request):
        id_token = request.data.get('id_token')
        phone_id_token = request.data.get('phone_id_token')
        if not id_token or not phone_id_token:
            return Response(
                {'error': 'id_token and phone_id_token are both required'},
                status=status.HTTP_400_BAD_REQUEST)

        social, error = verify_firebase_token(id_token)
        if error:
            return error
        phone_token, error = verify_firebase_token(phone_id_token)
        if error:
            return error

        # Only a phone sign-in carries this claim, so a second Google token
        # cannot stand in for the SMS step.
        canonical = to_e164(phone_token.get('phone_number'))
        if not canonical:
            return Response({'error': 'This sign-in carries no verified phone number'},
                            status=status.HTTP_400_BAD_REQUEST)

        uid = social['uid']
        email = (social.get('email') or '').strip().lower() or None
        name = social.get('name', '')

        user = resolve_social_user(uid, email)

        # The number may already belong to an account reached another way —
        # a password signup, or a different Google identity. Refusing is the
        # only safe answer: silently moving the number would hand this
        # caller the other account's recovery route.
        holder = User.objects.filter(phone=canonical).first()
        if holder is not None and (user is None or holder.pk != user.pk):
            return Response({'error': 'هذا الرقم مسجّل في حساب آخر'},
                            status=status.HTTP_409_CONFLICT)

        is_new_user = user is None
        with transaction.atomic():
            if is_new_user:
                user = User(
                    username=unique_username(email, name),
                    email=email,
                    first_name=name,
                    email_verified=bool(social.get('email_verified')),
                    firebase_uid=uid,
                    phone=canonical,
                    phone_verified=True,
                )
                # No password: this account signs in through Google, and an
                # unusable hash cannot be guessed or brute-forced.
                user.set_unusable_password()
                user.save()
            else:
                user.phone = canonical
                user.phone_verified = True
                if not user.firebase_uid:
                    user.firebase_uid = uid
                user.save(update_fields=['phone', 'phone_verified', 'firebase_uid'])

        logger.info(f"Phone attached to social account {user.id} (new={is_new_user})")
        return issue_session(user, is_new_user=is_new_user)


class FirebaseCustomTokenView(APIView):
    """
    Mint a Firebase Auth custom token for the current (JWT-authenticated) user,
    so mobile/web clients can sign in to Firebase Auth with the same identity
    and talk directly to Firestore (realtime chat) under Security Rules.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .firebase import get_firebase_app, FIREBASE_AVAILABLE

        if not FIREBASE_AVAILABLE or get_firebase_app() is None:
            return Response({
                'error': 'Firebase is not configured on the server'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            from firebase_admin import auth as firebase_auth
            token = firebase_auth.create_custom_token(str(request.user.id))
            return Response({'firebase_token': token.decode('utf-8')})
        except Exception as e:
            logger.error(f"Failed to mint Firebase custom token for user {request.user.id}: {e}")
            return Response({
                'error': 'Failed to generate Firebase token'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)