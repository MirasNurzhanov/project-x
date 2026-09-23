from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        from . import checks  # noqa: F401
        from .checks import private_media_problems

        # Registering the checks above is not enough on its own. The `web`
        # process is uvicorn, which never runs Django's check framework, so a
        # server with no private bucket would boot happily and fail one publish
        # at a time. Raising here is the only thing that stops that, and it has
        # to raise rather than log: a process that keeps serving after this is
        # a process quietly dropping every upload.
        problems = private_media_problems(settings.STORAGES, settings.DEBUG)
        if problems:
            raise ImproperlyConfigured('; '.join(
                f'[{identifier}] {message}' for identifier, message in problems
            ))
