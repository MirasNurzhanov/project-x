"""What the server knows about the app that sent a request.

One header, read in one place. The app sets it centrally in
`services/api.ts` — on the axios instance's request interceptor, alongside
`X-Approx-Presence`, and on the handful of calls that deliberately use bare axios
because they run before a session exists. No screen sets it by hand.

This exists because a server-side rule cannot be introduced to a shipped app
all at once. Build 31 is in App Review and in users' hands; it has never heard
of the Terms gate and does not send `accept_terms`. Enforcing against it would
lock out every existing user the moment the backend deployed — and the backend
has to be live *during* review, so "deploy after the release" is not available.

So the server asks which client it is talking to, and applies the rule to the
builds that understand it.

The header is client-asserted and therefore not a security boundary. That is
fine for what it decides: see `users.terms.terms_enforced_for`, which spells out
what a forged value can and cannot buy, and why the mitigation is closing the
window rather than trying to make the header unforgeable.
"""

#: WSGI form of `X-Approx-Build`.
CLIENT_BUILD_HEADER = 'HTTP_X_APPROX_BUILD'

#: What the same header was called before the app was renamed from Aura.
#:
#: Read as a fallback, and this is not tidiness — it is the whole point of the
#: mechanism above applied to itself. Every build already on a phone sends the
#: old name and will keep sending it until its owner updates, which for some of
#: them is never. Dropping the old name would make every one of those clients
#: look like a client that predates the Terms gate entirely, and silently move
#: the whole installed base onto the legacy path this module exists to shrink.
#:
#: Safe to delete once the oldest supported build sends the new name, which is
#: the same condition `TERMS_ENFORCEMENT_ALLOW_LEGACY` is waiting on.
LEGACY_CLIENT_BUILD_HEADER = 'HTTP_X_AURA_BUILD'


def client_build(request):
    """The app build that sent this request, or None if it did not say.

    None is a real answer and it is not the same as zero. It means "a client
    that predates this mechanism", which is precisely the population the
    compatibility window exists for — so callers must branch on `is None`
    rather than compare a defaulted number. Defaulting an absent header to the
    current build would enforce against the very clients that cannot comply,
    and defaulting it to 0 would silently exempt any future client that forgot
    to set it.

    Anything unparseable is treated as absent rather than as an error: a
    proxy that mangles the header should degrade to the legacy path, not
    produce a 500 on the login endpoint.
    """
    raw = request.META.get(CLIENT_BUILD_HEADER)
    if raw is None:
        raw = request.META.get(LEGACY_CLIENT_BUILD_HEADER)
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None
