import asyncio
import json
import random
import re
import secrets
import time
import urllib.error
import urllib.request


class UpstreamError(Exception):
    def __init__(self, service, status):
        super().__init__(f'{service}: HTTP {status}')
        self.status = status
        self.service = service


class AbortError(Exception):
    def __init__(self, message='The operation was aborted'):
        super().__init__(message)
        self.name = 'AbortError'


class AbortSignal:
    def __init__(self):
        self.aborted = False
        self._event = asyncio.Event()
        self.name = 'AbortSignal'
        self.deadline = None
        self._timer = None

    def abort(self):
        if not self.aborted:
            self.aborted = True
            self._event.set()

    def check(self):
        if self.aborted or (self.deadline is not None and time.monotonic() >= self.deadline):
            raise AbortError()

    def close(self):
        if self._timer:
            self._timer.cancel()

    @classmethod
    def timeout(cls, ms):
        sig = cls()
        loop = asyncio.get_running_loop()
        sig.deadline = time.monotonic() + ms / 1000
        sig._timer = loop.call_later(ms / 1000, sig.abort)
        return sig


class Headers:
    def __init__(self, data=None):
        self._d = {}
        if data:
            for key, value in dict(data).items():
                self._d[str(key).lower()] = value

    def get(self, key, default=None):
        return self._d.get(str(key).lower(), default)


class BodyStream:
    def __init__(self, data=b''):
        self._data = data if isinstance(data, (bytes, bytearray)) else data.encode('utf-8')
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def __aiter__(self):
        async def gen():
            if not self._cancelled and self._data:
                yield bytes(self._data)
        return gen()


class Response:
    def __init__(self, body='', init=None, **kwargs):
        init = {**(init or {}), **kwargs}
        if isinstance(body, str):
            body = body.encode('utf-8')
        elif body is None:
            body = b''
        self.status = init.get('status', 200)
        self.ok = 200 <= self.status < 300
        self.headers = Headers(init.get('headers'))
        self.body = BodyStream(body)

    @classmethod
    def json(cls, data, status=200, headers=None):
        hdrs = {'content-type': 'application/json', **(headers or {})}
        return cls(json.dumps(data), {'status': status, 'headers': hdrs})


class Blob:
    def __init__(self, parts, type=''):
        self.type = type
        raw = parts[0] if parts else ''
        self._data = raw

    async def text(self):
        return self._data if isinstance(self._data, str) else self._data.decode('utf-8')

    @property
    def data(self):
        return self._data


class FormData:
    def __init__(self):
        self._fields = []

    def set(self, name, value, filename=None):
        self._fields = [(n, v, f) for n, v, f in self._fields if n != name]
        self._fields.append((name, value, filename))

    def get(self, name):
        for n, v, _f in self._fields:
            if n == name:
                return v
        return None


class URLSearchParams(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect', headers, fp)


def encode_multipart(form):
    boundary = '----LaynAIForm' + secrets.token_hex(16)
    chunks = []
    for name, value, filename in form._fields:
        if any(c in str(name) + str(filename or '') for c in ('\r', '\n', '"')):
            raise ValueError('Nome de anexo inválido')
        chunks.append(f'--{boundary}\r\n'.encode('utf-8'))
        if filename:
            ctype = getattr(value, 'type', None) or 'application/octet-stream'
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f'Content-Type: {ctype}\r\n\r\n'.encode('utf-8')
            )
            raw = value.data if isinstance(value, Blob) else value
            chunks.append(raw.encode('utf-8') if isinstance(raw, str) else raw)
            chunks.append(b'\r\n')
        else:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode('utf-8'))
            chunks.append(str(value).encode('utf-8'))
            chunks.append(b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def _sync_fetch(url, options):
    options = options or {}
    headers = {str(k): str(v) for k, v in dict(options.get('headers') or {}).items()}
    body = options.get('body')
    method = options.get('method', 'GET')
    if options.get('redirect', 'error') != 'error':
        raise TypeError('Only redirect=error is supported')
    if isinstance(body, FormData):
        data, content_type = encode_multipart(body)
        headers['content-type'] = content_type
    elif isinstance(body, URLSearchParams):
        from urllib.parse import urlencode
        data = urlencode(body).encode('utf-8')
    elif isinstance(body, str):
        data = body.encode('utf-8')
    else:
        data = body
    timeout = float(options.get('timeout') or 30)
    signal = options.get('signal')
    if signal is not None:
        signal.check()
        if signal.deadline is not None:
            timeout = max(0.001, min(timeout, signal.deadline - time.monotonic()))
    limit = int(options.get('max_bytes', 1024 * 1024))

    def read_limited(stream):
        chunks, size = [], 0
        while True:
            if signal is not None:
                signal.check()
            chunk = stream.read(min(65536, limit + 1 - size))
            if not chunk:
                return b''.join(chunks)
            size += len(chunk)
            if size > limit:
                raise RuntimeError('Resposta excede o limite')
            chunks.append(chunk)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = read_limited(resp)
            status = resp.status
            hdrs = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        # Error bodies can contain credentials or user data and are never needed.
        raw = b''
        status = e.code
        hdrs = dict(e.headers.items()) if e.headers else {}
        e.close()
        if status in (301, 302, 303, 307, 308) and str(e.reason) == 'redirect':
            raise TypeError('URI requested responds with a redirect') from e
    if signal is not None and getattr(signal, 'aborted', False):
        raise AbortError()
    return Response(raw, {'status': status, 'headers': hdrs})


async def default_fetch(url, options=None):
    return await asyncio.to_thread(_sync_fetch, url, options or {})


async def with_signal(awaitable, signal):
    """Cancel the waiter promptly; underlying sockets also have bounded timeouts."""
    if signal is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    waiter = None
    try:
        signal.check()
        waiter = asyncio.create_task(signal._event.wait())
        done, _ = await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        signal.check()
        if waiter in done:
            raise AbortError()
        return await task
    finally:
        for item in (task, waiter):
            if item and not item.done():
                item.cancel()
        await asyncio.gather(*(item for item in (task, waiter) if item), return_exceptions=True)


async def _sleep(delay_ms, signal=None):
    if signal is not None and signal.aborted:
        raise AbortError()
    if signal is None:
        await asyncio.sleep(delay_ms / 1000)
        return
    try:
        await asyncio.wait_for(signal._event.wait(), timeout=delay_ms / 1000)
    except TimeoutError:
        return
    raise AbortError()


async def request(url, options=None, *, service='upstream', attempts=3, signal=None, fetcher=None):
    options = options or {}
    fetcher = fetcher or default_fetch
    if attempts < 1:
        raise ValueError('attempts deve ser positivo')
    for n in range(attempts):
        # Network failures are not retried: a POST might already have been billed.
        if signal is not None:
            signal.check()
        res = await with_signal(fetcher(url, {**options, 'signal': signal, 'redirect': 'error'}), signal)
        if res.ok:
            return res
        safe_retry = options.get('method', 'GET').upper() in ('GET', 'HEAD', 'PUT', 'DELETE', 'PATCH')
        transient = res.status == 429 or (safe_retry and res.status in (502, 503, 504))
        retry = res.headers.get('retry-after') if res.headers else None
        cancel = getattr(getattr(res, 'body', None), 'cancel', None)
        if cancel:
            maybe = cancel()
            if asyncio.iscoroutine(maybe):
                await maybe
        if not transient or n == attempts - 1:
            raise UpstreamError(service, res.status)
        retry_ms = 0
        if retry:
            if re.fullmatch(r'\d+(\.\d+)?', str(retry)):
                retry_ms = float(retry) * 1000
            else:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                try:
                    retry_ms = parsedate_to_datetime(retry).timestamp() * 1000 - datetime.now(timezone.utc).timestamp() * 1000
                except (TypeError, ValueError, OverflowError):
                    retry_ms = float('nan')
        delay = max(500 * (2 ** n) + random.random() * 250, retry_ms if retry_ms == retry_ms else 0)
        if delay > 30000:
            raise UpstreamError(service, res.status)
        await _sleep(delay, signal)


async def json_body(res, max_bytes=1024 * 1024):
    size = 0
    chunks = []
    async for chunk in res.body:
        size += len(chunk)
        if size > max_bytes:
            raise RuntimeError('Resposta excede o limite')
        chunks.append(chunk)
    return json.loads(b''.join(chunks).decode('utf-8'))
