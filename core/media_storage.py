"""Private new uploads with legacy object-name compatibility. No public fallback."""
from django.core.files.storage import Storage, storages
from django.utils.deconstruct import deconstructible


@deconstructible
class ProtectedMediaStorage(Storage):
    prefix = 'private/'

    def save(self, name, content, max_length=None):
        name = name if name.startswith(self.prefix) else self.prefix + name
        return super().save(name, content, max_length=max_length)

    def _backend(self, name):
        return storages['private_media'] if name.startswith(self.prefix) else storages['default']

    def _save(self, name, content):
        name = name if name.startswith(self.prefix) else self.prefix + name
        return storages['private_media'].save(name, content)

    def _open(self, name, mode='rb'):
        return self._backend(name).open(name, mode)

    def exists(self, name):
        return self._backend(name).exists(name)

    def url(self, name):
        return self._backend(name).url(name)

    def size(self, name):
        return self._backend(name).size(name)

    def delete(self, name):
        return self._backend(name).delete(name)


protected_media = ProtectedMediaStorage()
