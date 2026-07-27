from rest_framework import permissions


class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Custom permission to only allow owners to edit
    """

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True

        # Check if object has owner attribute
        if hasattr(obj, 'owner'):
            return obj.owner == request.user

        # For User objects
        return obj == request.user


class IsParticipantOrReadOnly(permissions.BasePermission):
    """
    Custom permission for chat participants
    """

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return obj.buyer == request.user or obj.seller == request.user

        return obj.buyer == request.user or obj.seller == request.user


class IsStoreOwner(permissions.BasePermission):
    """
    Only authenticated users with an active store may act; object-level
    checks that the target's store belongs to the requesting user.
    """
    message = 'You must own an active store to perform this action.'

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        store = getattr(request.user, 'store', None)
        return store is not None and store.is_active

    def has_object_permission(self, request, view, obj):
        from .models import UserStore
        store = obj if isinstance(obj, UserStore) else getattr(obj, 'store', None)
        return store is not None and store.user_id == request.user.id