"""Provider lookup.

The set of gateways is data, not branching. To add one: implement the class,
add a line here, add its credentials under PAYMENT_PROVIDERS in settings. A
provider missing its credentials stays out of `enabled_providers`, so a
half-configured gateway is never offered to a customer and then fails at
checkout.
"""
from .alqaseh import AlqasehProvider
from .base import ProviderError

PROVIDER_CLASSES = {
    AlqasehProvider.name: AlqasehProvider,
}

_instances = {}


def get_provider(name):
    """Returns the provider registered under [name].

    Raises [ProviderError] for an unknown name so a bad value from the app
    surfaces as a 400 rather than a 500.
    """
    key = (name or "").strip().lower()
    provider_class = PROVIDER_CLASSES.get(key)
    if provider_class is None:
        raise ProviderError("unknown payment provider %r" % name)
    if key not in _instances:
        _instances[key] = provider_class()
    return _instances[key]


def enabled_providers():
    """Configured providers only — what the payment-method picker shows."""
    out = []
    for name in PROVIDER_CLASSES:
        provider = get_provider(name)
        if provider.is_configured:
            out.append(provider)
    return out


def reset_cache():
    """Drops memoised instances. Tests use it after overriding settings."""
    _instances.clear()
