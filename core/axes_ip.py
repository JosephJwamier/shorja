"""Client IP for django-axes, resolved for our one-proxy deployment.

Every request reaches Django from Caddy, so REMOTE_ADDR is always Caddy's
container address — useless as a lockout key, since it is the same for every
visitor on earth. Caddy is the single edge proxy and appends the real peer as
the **right-most** entry of X-Forwarded-For, so that entry is the real client
and cannot be spoofed: whatever a client puts in X-Forwarded-For, Caddy's
appended value sits to the right of it.

ipware's proxy_count math did not reliably return that entry here, so axes is
pointed straight at this function via AXES_CLIENT_IP_CALLABLE.
"""


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.META.get("REMOTE_ADDR")
