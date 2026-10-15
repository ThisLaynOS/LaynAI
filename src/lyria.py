import base64
import binascii
import json
from types import SimpleNamespace

from net import json_body, request, with_signal


MAX_AUDIO_BYTES = 9 * 1024 * 1024
JSON_LIMIT = 13 * 1024 * 1024


class MusicError(ValueError):
    """Safe to show to the user; never includes provider data or credentials."""


def _audio(data):
    if not isinstance(data, dict):
        raise MusicError('O Lyria retornou uma resposta inválida. Tente novamente mais tarde.')
    if data.get('status') in ('failed', 'cancelled', 'error', 'incomplete'):
        raise MusicError('O Lyria não conseguiu gerar essa música. Tente outro prompt.')
    # Vertex documents outputs; newer Interactions responses use model_output steps.
    parts = list(data.get('outputs') or [])
    for step in data.get('steps') or []:
        if isinstance(step, dict) and step.get('type') == 'model_output':
            parts.extend(step.get('content') or [])
    for part in parts:
        if not isinstance(part, dict) or part.get('type') != 'audio':
            continue
        encoded = part.get('data')
        if part.get('mime_type') not in ('audio/mpeg', 'audio/mp3') or not isinstance(encoded, str):
            raise MusicError('O Lyria não retornou um arquivo MP3 válido. Tente outro prompt.')
        if len(encoded) > 4 * ((MAX_AUDIO_BYTES + 2) // 3):
            raise MusicError('A música ficou grande demais para enviar no Discord. Peça uma música mais curta.')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise MusicError('O Lyria retornou um áudio inválido. Tente novamente mais tarde.') from error
        if not raw or not (raw.startswith(b'ID3') or (len(raw) >= 2 and raw[0] == 0xff and raw[1] & 0xe0 == 0xe0)):
            raise MusicError('O Lyria retornou um áudio inválido. Tente novamente mais tarde.')
        if len(raw) > MAX_AUDIO_BYTES:
            raise MusicError('A música ficou grande demais para enviar no Discord. Peça uma música mais curta.')
        return SimpleNamespace(data=raw, mime='audio/mpeg', filename='musica.mp3', caption='Aqui está sua música!')
    raise MusicError('O Lyria não retornou áudio para esse pedido. Tente outro prompt.')


class Lyria:
    def __init__(self, cfg, auth, fetcher=None):
        self.cfg = cfg
        self.auth = auth
        self.fetcher = fetcher

    async def generate(self, prompt, signal=None):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise MusicError('Descreva a música em um prompt de até 4000 caracteres.')
        token = await with_signal(self.auth.token(), signal)
        # A generation POST must not be repeated: it may already have been billed.
        res = await request(self.cfg.musicEndpoint, {
            'method': 'POST',
            'headers': {
                'authorization': f'Bearer {token}',
                'content-type': 'application/json',
                'x-goog-user-project': self.cfg.project,
            },
            'body': json.dumps({
                'model': self.cfg.musicModel,
                'input': [{'type': 'text', 'text': prompt.strip()}],
            }, separators=(',', ':'), ensure_ascii=False),
            'timeout': self.cfg.musicTimeout / 1000,
            'max_bytes': JSON_LIMIT,
        }, attempts=1, signal=signal, fetcher=self.fetcher, service='lyria')
        data = await json_body(res, max_bytes=JSON_LIMIT)
        if signal:
            signal.check()
        return _audio(data)
