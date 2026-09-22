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

        # Send verification email
        send_email_verification_code(user)

        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)

        return Response({
            'user': UserSerializer(user).data,
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'message': 'Registration successful. Please verify your email.',
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

        username_or_email = serializer.validated_data['username']
        password = serializer.validated_data['password']

        # Try to find user by username or email
        user = None
        if '@' in username_or_email:
            try:
                user = User.objects.get(email=username_or_email)
                username = user.username
            except User.DoesNotExist:
                pass
        else:
            username = username_or_email

        # Authenticate
        user = authenticate(request, username=username if not user else user.username, password=password)

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

        # One phone, one verified account: otherwise several accounts could
        # each claim the same number and each be treated as reachable at it.
        taken = User.objects.filter(
            phone_verified=True, phone__endswith=account_phone
        ).exclude(pk=user.pk).exists()
        if taken:
            return Response({
                'error': 'This number is already verified on another account'
            }, status=status.HTTP_409_CONFLICT)

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
        email = request.data.get('email')

        try:
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

        email = serializer.validated_data['email']
        code = serializer.validated_data['code']

        try:
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
        from .firebase import get_firebase_app, FIREBASE_AVAILABLE

        id_token = request.data.get('id_token')
        if not id_token:
            return Response({'error': 'id_token is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not FIREBASE_AVAILABLE or get_firebase_app() is None:
            return Response({
                'error': 'Firebase is not configured on the server'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        from firebase_admin import auth as firebase_auth

        try:
            decoded = firebase_auth.verify_id_token(id_token)
        except Exception as e:
            logger.warning(f"Social login token verification failed: {e}")
            return Response({'error': 'Invalid or expired token'}, status=status.HTTP_401_UNAUTHORIZED)

        uid = decoded['uid']
        email = decoded.get('email')
        name = decoded.get('name', '')
        email_verified = decoded.get('email_verified', False)

        user = User.objects.filter(firebase_uid=uid).first()
        is_new_user = False

        if user is None and email:
            user = User.objects.filter(email=email).first()
            if user is not None:
                user.firebase_uid = uid
                user.save(update_fields=['firebase_uid'])

        if user is None:
            if not email:
                return Response({
                    'error': 'Your account did not share an email address — please allow email access and try again'
                }, status=status.HTTP_400_BAD_REQUEST)

            username = self._unique_username(email, name)
            with transaction.atomic():
                user = User.objects.create(
                    username=username,
                    email=email,
                    first_name=name,
                    email_verified=email_verified,
                    firebase_uid=uid,
                )
                user.set_unusable_password()
                user.save(update_fields=['password'])
            is_new_user = True

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

    @staticmethod
    def _unique_username(email, name):
        """Derive a unique username from the email local-part (falling back to
        the display name), appending a numeric suffix on collision."""
        import re
        base = (email.split('@')[0] if email else name) or 'user'
        base = re.sub(r'[^a-zA-Z0-9_]', '', base).lower() or 'user'
        base = base[:25]

        username = base
        suffix = 1
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f"{base}{suffix}"
        return username


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