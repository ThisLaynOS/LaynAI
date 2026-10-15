import asyncio
import json
import random
import sys
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


class WebSocketAdapter:
    OPEN = 1
    CONNECTING = 0
    CLOSING = 2
    CLOSED = 3

    def __init__(self, url):
        self.url = str(url)
        self.readyState = self.CONNECTING
        self._listeners = {'message': [], 'error': [], 'close': []}
        self._ws = None
        self._close_code = 1006
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._run())

    def addEventListener(self, event, callback):
        self._listeners.setdefault(event, []).append(callback)

    def send(self, text):
        if self.readyState == self.OPEN and self._ws is not None:
            self._loop.create_task(self._ws.send(text))

    def close(self, code=1000, reason=''):
        self._close_code = code
        if self._ws is not None:
            self._loop.create_task(self._ws.close(code, reason))
        elif self.readyState != self.CLOSED:
            self.readyState = self.CLOSED
            event = type('Close', (), {'code': code})()
            for cb in list(self._listeners.get('close', [])):
                cb(event)

    async def _run(self):
        try:
            import websockets
            from discord import USER_AGENT
            async with websockets.connect(
                self.url,
                compression=None,
                max_size=16 * 1024 * 1024,
                user_agent_header=USER_AGENT,
            ) as ws:
                self._ws = ws
                self.readyState = self.OPEN
                async for message in ws:
                    data = message if isinstance(message, str) else message.decode('utf-8')
                    event = type('Msg', (), {'data': data})()
                    for cb in list(self._listeners.get('message', [])):
                        cb(event)
                self._close_code = 1000
        except Exception as e:
            code = getattr(e, 'code', None)
            if code is not None:
                self._close_code = code
            else:
                for cb in list(self._listeners.get('error', [])):
                    cb(type('Err', (), {})())
        finally:
            self.readyState = self.CLOSED
            event = type('Close', (), {'code': self._close_code})()
            for cb in list(self._listeners.get('close', [])):
                cb(event)


def _with_params(url):
    parsed = urlparse(str(url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query['v'] = '10'
    query['encoding'] = 'json'
    return urlunparse(parsed._replace(query=urlencode(query)))


class Gateway:
    def __init__(self, discord, on_event, log=lambda *a, **k: None, Socket=None, message_content=False):
        self.Socket = Socket or WebSocketAdapter
        self.discord = discord
        self.on_event = on_event
        self.log = log
        self.seq = None
        self.session = None
        self.resumeURL = None
        self.attempt = 0
        self.stopped = False
        self.ready = False
        self.acked = True
        self.ws = None
        self.heartbeatTimer = None
        self.reconnectTimer = None
        self.onFatal = None
        self.url = None
        self.intents = 1 | 512 | 4096 | (32768 if message_content else 0)

    async def start(self):
        info = await self.discord.api('/gateway/bot')
        if (info or {}).get('shards', 1) > 1:
            raise RuntimeError('Este bot suporta um shard; limite a instalação a servidores pequenos/médios')
        remaining = ((info or {}).get('session_start_limit') or {}).get('remaining')
        if remaining == 0:
            raise RuntimeError('Limite de sessões Discord esgotado')
        self.url = info['url']
        self.connect()

    def connect(self):
        if self.stopped:
            return
        url = _with_params(self.resumeURL if self.session and self.resumeURL else self.url)
        ws = self.ws = self.Socket(url)
        self.ready = False
        self.acked = True
        loop = asyncio.get_running_loop()
        hello_timeout = loop.call_later(15, lambda: ws.close(4000, 'hello timeout'))

        def on_message(e):
            if self.ws is not ws or self.stopped:
                return
            try:
                p = json.loads(e.data)
                if p.get('s') is not None:
                    self.seq = p['s']
                op = p.get('op')
                if op == 10:
                    hello_timeout.cancel()
                    self.beginHeartbeat(p['d']['heartbeat_interval'])
                    if self.session:
                        self.send(6, {'token': self.discord.token, 'session_id': self.session, 'seq': self.seq})
                    else:
                        self.send(2, {
                            'token': self.discord.token,
                            'intents': self.intents,
                            'properties': {'os': sys.platform, 'browser': 'laynai', 'device': 'laynai'},
                        })
                elif op == 11:
                    self.acked = True
                elif op == 1:
                    self.heartbeat()
                elif op == 7:
                    ws.close(4000, 'reconnect')
                elif op == 9:
                    if not p.get('d'):
                        self.session = None
                        self.resumeURL = None
                        self.seq = None
                    ws.close(4000, 'invalid session')
                elif op == 0:
                    if p.get('t') == 'READY':
                        self.session = p['d']['session_id']
                        self.resumeURL = p['d'].get('resume_gateway_url')
                        self.ready = True
                        self.attempt = 0
                    if p.get('t') == 'RESUMED':
                        self.ready = True
                        self.attempt = 0
                    task = asyncio.ensure_future(self._dispatch(p.get('t'), p.get('d')))
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
            except Exception:
                self.log('gateway_payload_failed')

        def on_error(_e):
            self.log('gateway_connection_error')
            ws.close()

        def on_close(e):
            hello_timeout.cancel()
            if self.ws is not ws:
                return
            if self.heartbeatTimer:
                self.heartbeatTimer.cancel()
            self.ready = False
            if self.stopped:
                return
            code = getattr(e, 'code', 1006)
            if code in (4004, 4010, 4011, 4012, 4013, 4014):
                self.log('gateway_fatal', code=code)
                self.stopped = True
                # Exit so the supervisor can alert/restart; invalid credentials need operator repair.
                if self.onFatal:
                    result = self.onFatal()
                    if asyncio.iscoroutine(result):
                        asyncio.ensure_future(result)
                return
            if code in (4007, 4009):
                self.session = None
                self.seq = None
                self.resumeURL = None
            delay = min(60000, 5000 * (2 ** min(self.attempt, 4))) + random.random() * 1000
            self.attempt += 1
            self.log('gateway_reconnecting', code=code, delay_ms=round(delay))
            self.reconnectTimer = loop.call_later(delay / 1000, self.connect)

        ws.addEventListener('message', on_message)
        ws.addEventListener('error', on_error)
        ws.addEventListener('close', on_close)

    async def _dispatch(self, typ, data):
        try:
            result = self.on_event(typ, data)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                await result
        except Exception:
            self.log('gateway_event_failed')

    def send(self, op, d):
        ws = self.ws
        if ws is not None and getattr(ws, 'readyState', None) == getattr(self.Socket, 'OPEN', 1):
            ws.send(json.dumps({'op': op, 'd': d}, separators=(',', ':')))

    def heartbeat(self):
        self.acked = False
        self.send(1, self.seq)

    def beginHeartbeat(self, interval):
        if self.heartbeatTimer:
            self.heartbeatTimer.cancel()
        loop = asyncio.get_running_loop()

        def tick():
            if not self.acked:
                self.ws.close(4000, 'heartbeat timeout')
                return
            self.heartbeat()
            self.heartbeatTimer = loop.call_later(interval / 1000, tick)

        self.heartbeatTimer = loop.call_later((random.random() * interval) / 1000, tick)

    def stop(self):
        self.stopped = True
        self.ready = False
        if self.reconnectTimer:
            self.reconnectTimer.cancel()
        if self.heartbeatTimer:
            self.heartbeatTimer.cancel()
        if self.ws:
            self.ws.close(1000, 'shutdown')
