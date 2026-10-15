import asyncio
import base64
import json
import os
import time
import math
from pathlib import Path

from net import URLSearchParams, json_body, request


def _env_get(env, key):
    getter = getattr(env, 'get', None)
    value = getter(key) if getter else (env[key] if key in env else None)
    return value or None


def _b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(',', ':')).encode('utf-8')).rstrip(b'=').decode('ascii')


def _sign_jwt(payload, pem):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(pem.encode('utf-8'), password=None)
    sig = key.sign(payload.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
    return base64.urlsafe_b64encode(sig).rstrip(b'=').decode('ascii')


class GoogleAuth:
    def __init__(self, env=None, fetcher=None):
        self.env = os.environ if env is None else env
        self.fetcher = fetcher
        self.cached = None
        self.expires = 0
        self.pending = None

    async def token(self):
        if self.cached and self.expires > time.time() * 1000 + 60000:
            return self.cached
        if self.pending is None:
            self.pending = asyncio.get_running_loop().create_task(self._refresh_wrap())
        # Cancelling one generation must not cancel a token refresh shared by others.
        return await asyncio.shield(self.pending)

    async def _refresh_wrap(self):
        try:
            return await self.refresh()
        finally:
            self.pending = None

    async def refresh(self):
        from net import AbortSignal
        credentials = None
        cred_file = _env_get(self.env, 'GOOGLE_APPLICATION_CREDENTIALS') or ''
        cloudsdk = _env_get(self.env, 'CLOUDSDK_CONFIG')
        file = cred_file or str(Path(cloudsdk or str(Path.home() / '.config/gcloud')) / 'application_default_credentials.json')
        try:
            credentials = json.loads(Path(file).read_text(encoding='utf-8'))
        except FileNotFoundError:
            if cred_file:
                raise RuntimeError('Não foi possível ler as credenciais Google')
        except OSError:
            raise RuntimeError('Não foi possível ler as credenciais Google')
        except json.JSONDecodeError:
            raise RuntimeError('Não foi possível ler as credenciais Google')
        if credentials:
            url = 'https://oauth2.googleapis.com/token'
            if credentials.get('type') == 'service_account':
                now = int(time.time())
                payload = f"{_b64url({'alg':'RS256','typ':'JWT'})}.{_b64url({'iss':credentials['client_email'],'scope':'https://www.googleapis.com/auth/cloud-platform','aud':url,'iat':now,'exp':now+3600})}"
                sig_b64 = await asyncio.to_thread(_sign_jwt, payload, credentials['private_key'])
                jwt = f"{payload}.{sig_b64}"
                form = {'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': jwt}
            elif credentials.get('type') == 'authorized_user':
                form = {
                    'grant_type': 'refresh_token',
                    'client_id': credentials['client_id'],
                    'client_secret': credentials['client_secret'],
                    'refresh_token': credentials['refresh_token'],
                }
            else:
                raise RuntimeError('Tipo de credencial não suportado; use service_account, ADC local ou identidade da VM/Cloud Run')
            options = {'method': 'POST', 'headers': {'content-type': 'application/x-www-form-urlencoded'}, 'body': URLSearchParams(form)}
        else:
            url = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
            options = {'headers': {'Metadata-Flavor': 'Google'}}
        signal = AbortSignal.timeout(10000)
        try:
            result = await json_body(await request(url, options, signal=signal, fetcher=self.fetcher, service='google-auth', attempts=2))
        finally:
            signal.close()
        expiry = float(result.get('expires_in') or 0)
        if not isinstance(result.get('access_token'), str) or not result['access_token'] or not math.isfinite(expiry) or expiry <= 0:
            raise RuntimeError('Token Google inválido')
        self.cached = result['access_token']
        self.expires = time.time() * 1000 + expiry * 1000
        return self.cached
