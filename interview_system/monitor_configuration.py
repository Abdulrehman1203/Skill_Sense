"""Availability shared by the interview API and the monitor socket."""
from django.conf import settings


def relay_configured():
    urls = settings.LIVE_MONITOR_TURN_URLS
    return bool(settings.LIVE_MONITOR_TURN_SECRET and urls
                and all(url.startswith(('turn:', 'turns:')) for url in urls))


def monitor_configuration_status():
    if not settings.LIVE_MONITOR_ENABLED:
        return 'monitor_unavailable'
    direct_development = (settings.DEBUG and settings.LIVE_MONITOR_ALLOW_DIRECT
                          and not settings.LIVE_MONITOR_RELAY_ONLY
                          and not settings.LIVE_MONITOR_TURN_URLS
                          and not settings.LIVE_MONITOR_TURN_SECRET)
    return 'available' if relay_configured() or direct_development else 'relay_unavailable'
