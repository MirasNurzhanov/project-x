"""The three legal documents, served by Approx's own backend.

Not a marketing site. The app links these from the sign-up gate, from the
mandatory agreement screen and from Settings, so they have to resolve on a host
that exists and that ships with the code referencing them - and at the time this
was written there was no Approx domain to point at. `LEGAL_BASE_URL` in settings
is what a future marketing domain would change, without touching any of this.

Public, cacheable, and template-rendered rather than DRF: they are documents,
not API responses, and a reviewer opening one from a phone should get HTML.
"""
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.cache import cache_control

from users import terms


def _context(doc):
    return {
        'doc': doc,
        'version': terms.CURRENT_VERSION,
        'contact_email': getattr(
            settings, 'SAFETY_CONTACT_EMAIL', 'nurdauletaskar551@gmail.com'),
    }


# An hour. Long enough that the app's in-app browser does not re-fetch on every
# open, short enough that publishing a correction reaches people the same day.
_cache = cache_control(public=True, max_age=3600)


@_cache
def terms_page(request):
    return render(request, 'legal/terms.html', _context('terms'))


@_cache
def guidelines_page(request):
    return render(request, 'legal/guidelines.html', _context('guidelines'))


@_cache
def privacy_page(request):
    return render(request, 'legal/privacy.html', _context('privacy'))
