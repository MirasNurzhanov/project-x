"""Private new uploads with legacy object-name compatibility. No public fallback."""
import logging

from django.core.files.storage import Storage, storages
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)


@deconstructible
class ProtectedMediaStorage(Storage):
    prefix = 'private/'

    def save(self, name, content, max_length=None):
        name = name if name.startswith(self.prefix) else self.prefix + name
        return super().save(name, content, max_length=max_length)

    def _backend(self, name):
        return storages['private_media'] if name.startswith(self.prefix) else storages['default']

    def _purpose(self, name):
        """Which bucket an operation is against, as a word safe to log.

        The *purpose*, never the bucket name: the name is configuration, and
        `core.structured_logging` drops anything that is not identifier-shaped
        anyway. 'private' or 'public' is what an operator reading a failure
        needs -- it says whether a write went where it was meant to.
        """
        return 'private' if name.startswith(self.prefix) else 'public'

    def _attempt(self, operation, purpose, call):
        """Run a backend call, log what failed, and re-raise.

        Re-raises deliberately. A storage error that this layer swallowed would
        leave the caller holding a model whose file field points at nothing,
        and `SwiftCreateSerializer.save` would go on to commit the row -- a
        Swift in the database with no video behind it is worse than a failed
        publish. The log exists so the 500 is diagnosable, not so it can be
        ignored: before this, an operator saw only `status_code: 500` with no
        indication that storage was even involved.

        No credentials and no object paths: the operation, which bucket it was
        for, the exception class, and the request id the formatter attaches.
        """
        try:
            return call()
        except Exception as error:
            logger.error(
                'media_storage_operation_failed',
                extra={
                    'operation': operation,
                    'bucket': purpose,
                    'error_type': type(error).__name__,
                },
                exc_info=True,
            )
            raise

    def _save(self, name, content):
        # Unconditionally the private backend, never `_backend`. Reads fall back
        # to the public bucket for legacy Cloudinary-era object names (see
        # `_backend`), and a write that inherited that fallback would publish a
        # chat image or a Swift to a world-readable bucket on the strength of a
        # missing prefix.
        name = name if name.startswith(self.prefix) else self.prefix + name
        return self._attempt('save', 'private', lambda: storages['private_media'].save(name, content))

    def _open(self, name, mode='rb'):
        return self._attempt('open', self._purpose(name), lambda: self._backend(name).open(name, mode))

    def exists(self, name):
        return self._attempt('exists', self._purpose(name), lambda: self._backend(name).exists(name))

    def url(self, name):
        return self._attempt('url', self._purpose(name), lambda: self._backend(name).url(name))

    def size(self, name):
        return self._attempt('size', self._purpose(name), lambda: self._backend(name).size(name))

    def delete(self, name):
        return self._attempt('delete', self._purpose(name), lambda: self._backend(name).delete(name))


protected_media = ProtectedMediaStorage()
