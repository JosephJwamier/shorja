"""
ReSell Trust - Authentication URLs (core/auth_urls.py)
Custom authentication endpoints
"""

from django.urls import path
from .otp_views import (
    SendPhoneCodeView,
    VerifyPhoneCodeView,
    ResetPasswordByCodeView,
)
from .auth_views import (
    RegisterView,
    LoginView,
    LogoutView,
    VerifyEmailView,
    ResendVerificationView,
    ChangePasswordView,
    ResetPasswordView,
    ResetPasswordConfirmView,
    UserProfileView,
    UpdateProfileView,
    FirebaseCustomTokenView,
    SocialLoginView,
    SocialAttachPhoneView,
)

urlpatterns = [
    # Registration & Login
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('social-login/', SocialLoginView.as_view(), name='social-login'),
    # Second half of a Google sign-in when the account has no verified
    # phone yet — social-login/ answers 'phone_required' and writes nothing.
    path('social-login/attach-phone/', SocialAttachPhoneView.as_view(),
         name='social-attach-phone'),

    # Email Verification
    path('verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('resend-verification/', ResendVerificationView.as_view(), name='resend-verification'),

    # Phone Verification — رمز واتساب عبر Wevlix. الخادم يطلب الإرسال
    # ويتحقّق، بعد أن كانت Firebase ترسل من الجهاز بلا علمنا.
    path('phone/send-code/', SendPhoneCodeView.as_view(), name='phone-send-code'),
    path('phone/verify-code/', VerifyPhoneCodeView.as_view(), name='phone-verify-code'),
    path('reset-password-code/', ResetPasswordByCodeView.as_view(),
         name='reset-password-code'),

    # Password Management
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('reset-password/', ResetPasswordView.as_view(), name='reset-password'),
    path('reset-password-confirm/', ResetPasswordConfirmView.as_view(), name='reset-password-confirm'),

    # Profile
    path('profile/', UserProfileView.as_view(), name='profile'),
    path('profile/update/', UpdateProfileView.as_view(), name='update-profile'),

    # Firebase Auth bridge (for Firestore realtime chat)
    path('firebase-token/', FirebaseCustomTokenView.as_view(), name='firebase-token'),
]