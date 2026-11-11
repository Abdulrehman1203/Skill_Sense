"""Fail-fast production Clerk configuration; no provider/network calls at startup."""
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured

def validate(values):
    for key in ('CLERK_ISSUER','CLERK_JWKS_URL','CLERK_SECRET_KEY','CLERK_WEBHOOK_SECRET','CLERK_AUTHORIZED_PARTIES'):
        if not values.get(key):
            raise ImproperlyConfigured('Required production auth setting: ' + key)
    issuer=values['CLERK_ISSUER']; parsed=urlsplit(issuer)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or '*' in issuer or any(c.isspace() for c in issuer):
        raise ImproperlyConfigured('CLERK_ISSUER must be an exact HTTPS origin.')
    if values['CLERK_JWKS_URL'] != issuer + '/.well-known/jwks.json':
        raise ImproperlyConfigured('CLERK_JWKS_URL must belong to the configured issuer.')
    parties=values['CLERK_AUTHORIZED_PARTIES']
    if not isinstance(parties,(list,tuple)) or not parties:
        raise ImproperlyConfigured('Production authorized parties must be explicitly configured.')
    for party in parties:
        p=urlsplit(party)
        if p.scheme!='https' or not p.hostname or p.path or p.query or p.fragment or p.username or p.password or '*' in party:
            raise ImproperlyConfigured('Authorized parties must be exact HTTPS origins.')
