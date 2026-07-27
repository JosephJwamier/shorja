"""Wired via UNFOLD['DASHBOARD_CALLBACK'] in settings.py — Unfold calls this
on every admin index render and merges what it returns into the template
context (see unfold.sites.UnfoldAdminSite.index)."""
from .services.analytics import get_dashboard_data


def dashboard_callback(request, context):
    context['dashboard'] = get_dashboard_data()
    return context
