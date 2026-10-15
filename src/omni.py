import asyncio
import base64
import json

from net import json_body, request


def _extract_video(data):
    for step in data.get('steps') or []:
        for part in step.get('content') or []:
            if not isinstance(part, dict):
                continue
            if part.get('type') != 'video':
                continue
            raw = part.get('data')
            if raw:
                mime = part.get('mime_type') or part.get('mimeType') or 'video/mp4'
                return base64.b64decode(raw), mime
    video = data.get('output_video') or {}
    if video.get('data'):
        mime = video.get('mime_type') or video.get('mimeType') or 'video/mp4'
        return base64.b64decode(video['data']), mime
    return None, None


_JSON_LIMIT = 48 * 1024 * 1024
_DISCORD_LIMIT = 9 * 1024 * 1024


def _seconds(duration):
    try:
        secs = int(duration or 6)
    except (TypeError, ValueError):
        secs = 6
    return secs if secs in (4, 6, 8) else 6


def _fail_text(data):
    return 'O serviço não conseguiu gerar o vídeo. Tente um pedido diferente.'


class Omni:
    def __init__(self, cfg, auth, fetcher=None):
        self.cfg = cfg
        self.auth = auth
        self.fetcher = fetcher

    async def generate(self, prompt, aspect_ratio='16:9', duration=6, signal=None):
        ratio = aspect_ratio if aspect_ratio in ('16:9', '9:16') else '16:9'
        secs = _seconds(duration)
        # 360p cabe no limite de anexo do Discord. 720p em clipe com áudio passa de 10 MB
        # e a resposta JSON estoura antes de chegar na mensagem.
        body = json.dumps({
            'model': self.cfg.videoModel,
            'input': prompt,
            'background': True,
            'response_format': {
                'type': 'video',
                'aspect_ratio': ratio,
                'resolution': '360p',
                'duration': f'{secs}s',
            },
        }, separators=(',', ':'), ensure_ascii=False)
        res = await self._call(self.cfg.videoEndpoint, 'POST', body, signal, timeout=90)
        data = await self._read(res)
        op_id = data.get('id')
        status = (data.get('status') or '').lower()
        tries = 0
        while status in ('in_progress', 'queued', 'running', 'processing', '') and op_id and tries < 40:
            blob, mime = _extract_video(data)
            if blob:
                break
            await self._wait(signal, 5)
            tries += 1
            poll = await self._call(f"{self.cfg.videoEndpoint}/{op_id}", 'GET', None, signal, timeout=120)
            data = await self._read(poll)
            status = (data.get('status') or '').lower()
            blob, mime = _extract_video(data)
            if blob:
                break
        else:
            blob, mime = _extract_video(data)
        if status in ('failed', 'cancelled', 'error'):
            raise RuntimeError(_fail_text(data))
        if not blob:
            raise RuntimeError('O Omni Flash não retornou vídeo. Tente de novo em instantes.')
        if len(blob) > _DISCORD_LIMIT:
            raise RuntimeError('O vídeo ficou grande demais para o Discord (limite ~10 MB). Tente um pedido mais simples.')
        return type('Video', (), {
            'data': blob,
            'mime': mime or 'video/mp4',
            'filename': 'video.mp4',
            'caption': 'Aqui está o vídeo.',
        })()

    async def _call(self, url, method, body, signal, timeout):
        last = None
        # GET de consulta pode repetir. POST não: a geração já pode ter sido cobrada.
        attempts = 3 if method == 'GET' else 1
        for n in range(attempts):
            headers = await self._headers()
            options = {'method': method, 'headers': headers, 'timeout': timeout, 'max_bytes': _JSON_LIMIT}
            if body is not None:
                options['body'] = body
            try:
                return await request(url, options, signal=signal, fetcher=self.fetcher, service='omni', attempts=1)
            except (TimeoutError, OSError) as e:
                last = e
                if signal is not None and getattr(signal, 'aborted', False):
                    raise
                if n == attempts - 1:
                    raise
                await self._wait(signal, 2)
        raise last

    async def _read(self, res):
        try:
            return await json_body(res, max_bytes=_JSON_LIMIT)
        except RuntimeError as e:
            if 'excede' in str(e):
                raise RuntimeError('O vídeo ficou grande demais para o Discord (limite ~10 MB). Tente um pedido mais simples.') from e
            raise

    async def _wait(self, signal, seconds):
        if signal is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(signal._event.wait(), timeout=seconds)
        except TimeoutError:
            return
        from net import AbortError
        raise AbortError()

    async def _headers(self):
        token = await self.auth.token()
        return {
            'authorization': f'Bearer {token}',
            'content-type': 'application/json',
            'x-goog-user-project': self.cfg.project,
        }
