"""One JSON record per line, with no request bodies or exception messages."""
import json
import logging
import re
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .credential_logging import redact

request_id = ContextVar('request_id', default=None)


class JSONFormatter(logging.Formatter):
    def format(self, record):
        event = redact(record.getMessage())[:1000]
        # Framework access/security messages may contain arbitrary user paths,
        # client addresses or invalid headers. Keep their category, not payload.
        if record.name == 'uvicorn.access':
            event = 'http_access'
        elif record.name.startswith('django.request'):
            event = 'django_request'
        elif record.name.startswith('django.security'):
            event = 'django_security_event'
        elif record.name.startswith('uvicorn') and 'WebSocket' in str(record.msg):
            event = 'websocket_event'
        data = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'event': event,
            'request_id': request_id.get() or getattr(getattr(record, 'request', None), 'aura_request_id', None),
        }
        for key in ('count', 'status_code', 'duration_ms'):
            value = getattr(record, key, None)
            if isinstance(value, (int, float)):
                data[key] = value
        # Identifier-shaped strings only, for the same reason `event` above is
        # redacted and truncated: every one of these is a label chosen in code --
        # an exception class, a storage operation, which bucket a write was for --
        # and never a path, a payload or a credential. The pattern is the
        # enforcement; a caller that passes a bucket *name* or a URL here gets it
        # dropped rather than logged.
        for key in ('error_type', 'operation', 'bucket'):
            value = getattr(record, key, None)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,100}', value):
                data[key] = value
        if record.exc_info:
            data['error_type'] = record.exc_info[0].__name__
            # Retain actionable source locations, never exception text/locals.
            data['trace'] = [
                {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])[-20:]
            ]
        return json.dumps(data, ensure_ascii=True, allow_nan=False)
