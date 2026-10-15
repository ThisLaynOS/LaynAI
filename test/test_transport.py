import asyncio
import base64
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gateway import Gateway
from google_auth import GoogleAuth
from net import Response, request


class FakeSocket:
    OPEN = 1

    def __init__(self, url):
        self.url = str(url)
        self.sent = []
        self.readyState = 1
        self._listeners = {'message': [], 'error': [], 'close': []}

    def addEventListener(self, event, cb):
        self._listeners.setdefault(event, []).append(cb)

    def send(self, text):
        self.sent.append(json.loads(text))

    def receive(self, data):
        event = type('E', (), {'data': json.dumps(data)})()
        for cb in list(self._listeners.get('message', [])):
            cb(event)

    def close(self, code=1000, reason=''):
        self.readyState = 3
        event = type('E', (), {'code': code})()
        for cb in list(self._listeners.get('close', [])):
            cb(event)


class TransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_service_account_jwt_signature_claims_single_flight_and_token_cache(self):
        # Exercise real RSA signing while isolating executor scheduling from the
        # authentication contract (the network transport is also injected below).
        from unittest.mock import patch
        async def inline_thread(function, *args):
            return function(*args)
        threaded = patch('google_auth.asyncio.to_thread', inline_thread)
        threaded.start()
        self.addCleanup(threaded.stop)
        dir_ = tempfile.mkdtemp(prefix='laynai-auth-')
        self.addCleanup(lambda: shutil.rmtree(dir_, ignore_errors=True))
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode('utf-8')
        path = str(Path(dir_) / 'credentials.json')
        Path(path).write_text(json.dumps({
            'type': 'service_account',
            'client_email': 'bot@example.test',
            'private_key': pem,
        }), encoding='utf-8')
        calls = {'n': 0}

        async def fetcher(url, opts):
            calls['n'] += 1
            self.assertEqual(url, 'https://oauth2.googleapis.com/token')
            jwt = opts['body'].get('assertion')
            header, payload, sig = jwt.split('.')
            public_key.verify(
                base64.urlsafe_b64decode(sig + '=' * (-len(sig) % 4)),
                f'{header}.{payload}'.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            self.assertEqual(claims['iss'], 'bot@example.test')
            self.assertEqual(claims['exp'] - claims['iat'], 3600)
            return Response.json({'access_token': 'fresh', 'expires_in': 3600})

        auth = GoogleAuth({'GOOGLE_APPLICATION_CREDENTIALS': path}, fetcher)
        self.assertEqual(await asyncio.gather(auth.token(), auth.token()), ['fresh', 'fresh'])
        self.assertEqual(await auth.token(), 'fresh')
        self.assertEqual(calls['n'], 1)
        auth.expires = 0
        await auth.token()
        self.assertEqual(calls['n'], 2)

    async def test_local_user_adc_refresh_supported_unsupported_federation_fails(self):
        dir_ = tempfile.mkdtemp(prefix='laynai-auth-')
        self.addCleanup(lambda: shutil.rmtree(dir_, ignore_errors=True))
        path = str(Path(dir_) / 'adc.json')
        Path(path).write_text(json.dumps({
            'type': 'authorized_user',
            'client_id': 'id',
            'client_secret': 'secret',
            'refresh_token': 'refresh',
        }), encoding='utf-8')

        async def fetcher(_url, opts):
            self.assertEqual(opts['body'].get('refresh_token'), 'refresh')
            return Response.json({'access_token': 'ok', 'expires_in': 3600})

        auth = GoogleAuth({'GOOGLE_APPLICATION_CREDENTIALS': path}, fetcher)
        self.assertEqual(await auth.token(), 'ok')
        Path(path).write_text(json.dumps({'type': 'external_account'}), encoding='utf-8')
        with self.assertRaisesRegex(Exception, 'não suportado'):
            await GoogleAuth({'GOOGLE_APPLICATION_CREDENTIALS': path}).token()

    async def test_attached_service_account_uses_metadata_header(self):
        dir_ = tempfile.mkdtemp(prefix='laynai-auth-')
        self.addCleanup(lambda: shutil.rmtree(dir_, ignore_errors=True))

        async def fetcher(url, opts):
            self.assertRegex(url, r'metadata.google.internal')
            self.assertEqual(opts['headers']['Metadata-Flavor'], 'Google')
            return Response.json({'access_token': 'metadata-token', 'expires_in': 3600})

        auth = GoogleAuth({'CLOUDSDK_CONFIG': dir_}, fetcher)
        self.assertEqual(await auth.token(), 'metadata-token')

    async def test_gateway_identify_sequence_heartbeat_resume_and_shutdown(self):
        events = []

        async def on_event(typ, _data=None):
            events.append(typ)

        async def api(_path, *_a, **_k):
            return {'url': 'wss://gateway.discord.gg', 'shards': 1}

        g = Gateway(type('D', (), {'token': 'test', 'api': api})(), on_event, lambda *_a, **_k: None, FakeSocket)
        self.addCleanup(g.stop)
        await g.start()
        first = g.ws
        first.receive({'op': 10, 'd': {'heartbeat_interval': 60000}})
        self.assertEqual(first.sent[0]['op'], 2)
        self.assertEqual(first.sent[0]['d']['intents'], 4609)
        first.receive({'op': 0, 't': 'READY', 's': 5, 'd': {'session_id': 'session', 'resume_gateway_url': 'wss://gateway-us.discord.gg'}})
        self.assertEqual(g.ready, True)
        self.assertEqual(g.seq, 5)
        first.receive({'op': 1, 'd': None})
        self.assertEqual(first.sent[-1]['op'], 1)
        self.assertEqual(g.acked, False)
        first.receive({'op': 11, 'd': None})
        self.assertEqual(g.acked, True)
        first.receive({'op': 0, 't': 'MESSAGE_CREATE', 's': 6, 'd': {'content': 'hi'}})
        await asyncio.sleep(0)
        self.assertIn('MESSAGE_CREATE', events)
        first.receive({'op': 7, 'd': None})
        if g.reconnectTimer:
            g.reconnectTimer.cancel()
        g.connect()
        g.ws.receive({'op': 10, 'd': {'heartbeat_interval': 60000}})
        self.assertEqual(g.ws.sent[0]['op'], 6)
        self.assertEqual(g.ws.sent[0]['d']['seq'], 6)
        g.ws.receive({'op': 0, 't': 'RESUMED', 's': 7, 'd': {}})
        self.assertEqual(g.ready, True)
        g.stop()
        self.assertEqual(g.stopped, True)
        self.assertEqual(g.ready, False)

    async def test_gateway_invalid_nonresumable_session_clears_state(self):
        async def api(_path, *_a, **_k):
            return {'url': 'wss://gateway.discord.gg'}

        async def on_event(*_a, **_k):
            return None

        g = Gateway(type('D', (), {'token': 'test', 'api': api})(), on_event, lambda *_a, **_k: None, FakeSocket)
        self.addCleanup(g.stop)
        await g.start()
        g.ws.receive({'op': 10, 'd': {'heartbeat_interval': 60000}})
        g.session = 'old'
        g.seq = 99
        g.ws.receive({'op': 9, 'd': False})
        self.assertEqual(g.session, None)
        self.assertEqual(g.seq, None)

    async def test_retry_succeeds_after_transient_response_cancellation_interrupts_backoff(self):
        n = {'v': 0}

        async def flaky(_url, _opts):
            n['v'] += 1
            if n['v'] == 1:
                return Response('', {'status': 503})
            return Response.json({'ok': True})

        r = await request('https://example.test', {}, fetcher=flaky)
        self.assertEqual(n['v'], 2)
        self.assertEqual(r.status, 200)

        from net import AbortSignal
        async def always_429(_url, _opts):
            return Response('', {'status': 429})

        with self.assertRaisesRegex(Exception, r'abort'):
            await request('https://example.test', {}, signal=AbortSignal.timeout(10), fetcher=always_429)


if __name__ == '__main__':
    unittest.main()
