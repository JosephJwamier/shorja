"""Financier lookup — the same shape as services/payments/registry.py.

The set of financiers is data, not branching. To add one: implement the class,
add a line here, add its credentials under INSTALLMENT_PROVIDERS in settings. A
financier missing its credentials stays out of `enabled_providers`, so
installments are never offered to a buyer who would then hit a dead end at
confirmation.
"""
from .base import InstallmentError
from .qicard import QiCardProvider

PROVIDER_CLASSES = {
    QiCardProvider.name: QiCardProvider,
}

_instances = {}


def get_provider(name):
    """Returns the financier registered under [name].

    Raises [InstallmentError] for an unknown name so a bad value from the app
    surfaces as a 400 rather than a 500.
    """
    key = (name or "").strip().lower()
    provider_class = PROVIDER_CLASSES.get(key)
    if provider_class is None:
        raise InstallmentError("unknown installment provider %r" % name)
    if key not in _instances:
        _instances[key] = provider_class()
    return _instances[key]


def enabled_providers():
    """Configured financiers only — what the payment picker may show."""
    out = []
    for name in PROVIDER_CLASSES:
        provider = get_provider(name)
        if provider.is_configured:
            out.append(provider)
    return out


def default_provider():
    """The one financier offered today. None when none is configured.

    Returning a single provider rather than a list keeps the app's checkout
    free of a picker it does not need yet; `enabled_providers` is there for
    the day a second financier exists.
    """
    providers = enabled_providers()
    return providers[0] if providers else None


def reset_cache():
    """Drops memoised instances. Tests use it after overriding settings."""
    _instances.clear()
