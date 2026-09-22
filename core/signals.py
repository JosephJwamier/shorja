"""Model-level invariants that must hold no matter which code path wrote the row.

Kept as signals rather than serializer logic on purpose: items are created by
the API, by the admin, by wholesale transfers, by fixtures and by tests, and an
invariant enforced in only one of those places is not an invariant.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Item, ItemVariant, User


@receiver(post_save, sender=Item)
def ensure_default_variant(sender, instance, created, **kwargs):
    """Every item has at least one variant.

    A freshly created item gets a single default variant — empty attributes,
    carrying the quantity the creator set. Code that wants real options
    (ItemCreateSerializer) replaces it afterwards; code that never heard of
    variants gets exactly the pre-variant behaviour for free.

    Only on `created`: later saves come from sync_stock_from_variants() and
    must not spawn variants, or every stock change would grow the row count.
    """
    if not created:
        return
    if instance.variants.exists():
        return
    ItemVariant.objects.create(item=instance, attributes={}, quantity=instance.quantity)


@receiver(post_save, sender=User)
def keep_verified_badge_in_step(sender, instance, created, **kwargs):
    """The «موثَّق» badge is a fact about the owner's verification, so it
    changes the moment the flags do — not at the nightly sweep."""
    if created:
        return
    store = getattr(instance, 'store', None) if hasattr(instance, 'store') else None
    if store is None:
        return
    from .services.store_badges import sync_verified_badge
    sync_verified_badge(store)
