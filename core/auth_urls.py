"""
ReSell Trust - Authentication URLs (core/auth_urls.py)
Custom authentication endpoints
"""

from django.urls import path
from .auth_views import (
    RegisterView,
    LoginView,
    LogoutView,
    VerifyEmailView,
    VerifyPhoneView,
    ResendVerificationView,
    ChangePasswordView,
    ResetPasswordView,
    ResetPasswordConfirmView,
    ResetPasswordByPhoneView,
    UserProfileView,
    UpdateProfileView,
    FirebaseCustomTokenView,
    SocialLoginView,
)

urlpatterns = [
    # Registration & Login
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('social-login/', SocialLoginView.as_view(), name='social-login'),

    # Email Verification
    path('verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('resend-verification/', ResendVerificationView.as_view(), name='resend-verification'),

    # Phone Verification
    path('verify-phone/', VerifyPhoneView.as_view(), name='verify-phone'),

    # Password Management
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('reset-password/', ResetPasswordView.as_view(), name='reset-password'),
    path('reset-password-confirm/', ResetPasswordConfirmView.as_view(), name='reset-password-confirm'),
    # Phone route: the only one available to accounts without an email.
    path('reset-password-phone/', ResetPasswordByPhoneView.as_view(), name='reset-password-phone'),

    # Profile
    path('profile/', UserProfileView.as_view(), name='profile'),
    path('profile/update/', UpdateProfileView.as_view(), name='update-profile'),

    # Firebase Auth bridge (for Firestore realtime chat)
    path('firebase-token/', FirebaseCustomTokenView.as_view(), name='firebase-token'),
]