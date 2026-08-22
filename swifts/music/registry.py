"""Which providers exist, and which are usable right now."""
from .epidemic import EpidemicSoundProvider

#: Keyed by `MusicProvider.key`. A provider added here becomes available to
#: `sync_music` and appears in the provider status endpoint; nothing else has
#: to change.
PROVIDERS = {
    EpidemicSoundProvider.key: EpidemicSoundProvider(),
}

#: What `SwiftTrack.provider` holds for a row somebody added by hand in the
#: admin rather than importing. Not in PROVIDERS — there is nothing to sync.
LOCAL = 'local'


def get(key):
    provider = PROVIDERS.get(key)
    if provider is None:
        raise KeyError(f'unknown music provider: {key!r}')
    return provider


def status():
    """One line per provider, for the admin and the status endpoint.

    Names the *missing* settings rather than just saying "not configured", so
    the answer to "why is this off" is in the response instead of in a hunt
    across three variables.
    """
    return [
        {
            'key': key,
            'label': provider.label,
            'configured': provider.is_configured(),
            'missing': list(provider.missing_settings()),
        }
        for key, provider in PROVIDERS.items()
    ]
