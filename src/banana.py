import base64
import binascii
import json

from attachments import download_image
from net import UpstreamError, json_body, request, with_signal


class ImageGenerationError(ValueError):
    """User-safe generation failure, with a stable code for diagnostics."""

    def __init__(self, message, *, code='image_response', status=0):
        super().__init__(message)
        self.code = code
        self.status = status


def image_service_error(error):
    status = error.status
    if status == 429:
        message = 'O Nano Banana está no limite de uso. Tente novamente mais tarde.'
    elif status in (401, 403):
        message = 'O acesso ao Nano Banana precisa ser revisado no projeto Google pelo administrador.'
    elif status == 404:
        message = 'O modelo de imagem configurado não está disponível no projeto Google.'
    elif status == 400:
        message = 'O Nano Banana não aceitou a foto ou o pedido de edição. Tente outra instrução.'
    else:
        message = 'O serviço de imagem do Google falhou. Tente novamente mais tarde.'
    return ImageGenerationError(message, code='image_service', status=status)


def _result_parts(data):
    if not isinstance(data, dict):
        raise ImageGenerationError('O Nano Banana retornou uma resposta inválida.')
    feedback = data.get('promptFeedback') or {}
    candidates = data.get('candidates') or []
    if not isinstance(feedback, dict) or not isinstance(candidates, list):
        raise ImageGenerationError('O Nano Banana retornou uma resposta inválida.')
    candidate = candidates[0] if candidates else {}
    if not isinstance(candidate, dict):
        raise ImageGenerationError('O Nano Banana retornou uma resposta inválida.')
    finish = candidate.get('finishReason')
    if feedback.get('blockReason') or finish in ('SAFETY', 'IMAGE_SAFETY', 'PROHIBITED_CONTENT', 'RECITATION', 'IMAGE_RECITATION'):
        raise ImageGenerationError('O Nano Banana recusou essa edição. Tente outra instrução ou outra foto.',
                                   code='image_blocked')
    content = candidate.get('content') or {}
    if not isinstance(content, dict) or not isinstance(content.get('parts') or [], list):
        raise ImageGenerationError('O Nano Banana retornou uma resposta inválida.')
    return content.get('parts') or []


class Banana:
    def __init__(self, cfg, auth, fetcher=None):
        self.cfg = cfg
        self.auth = auth
        self.fetcher = fetcher

    async def generate(self, prompt, aspect_ratio=None, signal=None, *, attachment=None):
        parts = [{'text': prompt}]
        if attachment is not None:
            source = await download_image(attachment, signal=signal, fetcher=self.fetcher,
                                          timeout=min(30, self.cfg.timeout / 1000))
            parts.append({'inlineData': {'mimeType': source['mime'],
                                         'data': base64.b64encode(source['data']).decode('ascii')}})
        generation = {'responseModalities': ['TEXT', 'IMAGE']}
        # Let the model preserve the source proportions unless a format was requested.
        if aspect_ratio or attachment is None:
            generation['imageConfig'] = {'aspectRatio': aspect_ratio or '1:1'}
        try:
            token = await with_signal(self.auth.token(), signal)
            res = await self._generate(parts, generation, token, signal)
        except UpstreamError as error:
            raise image_service_error(error) from error
        try:
            data = await json_body(res, max_bytes=12 * 1024 * 1024)
        except (ValueError, RuntimeError) as error:
            raise ImageGenerationError('O Nano Banana retornou uma resposta inválida ou grande demais.') from error
        parts = _result_parts(data)
        caption = ''
        image = None
        mime = 'image/png'
        for part in parts:
            if not isinstance(part, dict) or part.get('thought'):
                continue
            text = part.get('text')
            if isinstance(text, str) and text.strip():
                caption = text.strip()
            blob = part.get('inlineData') or part.get('inline_data') or {}
            if not isinstance(blob, dict):
                raise ImageGenerationError('O Nano Banana retornou uma imagem inválida.')
            raw = blob.get('data')
            if raw:
                mime = blob.get('mimeType') or blob.get('mime_type') or 'image/png'
                try:
                    image = base64.b64decode(raw, validate=True)
                except (ValueError, TypeError, binascii.Error) as error:
                    raise ImageGenerationError('O Nano Banana retornou uma imagem inválida.') from error
        if not image:
            raise ImageGenerationError('O Nano Banana não devolveu uma imagem para esse pedido. Tente descrever a edição de outra forma.',
                                       code='image_missing')
        return type('Image', (), {
            'data': image,
            'mime': mime,
            'filename': 'imagem.png' if 'png' in mime else 'imagem.jpg',
            'caption': caption,
        })()

    async def _generate(self, parts, generation, token, signal):
        return await request(self.cfg.imageEndpoint, {
            'method': 'POST',
            'max_bytes': 12 * 1024 * 1024,
            'timeout': self.cfg.timeout / 1000,
            'headers': {
                'authorization': f'Bearer {token}',
                'content-type': 'application/json',
                'x-goog-user-project': self.cfg.project,
            },
            'body': json.dumps({
                'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': generation,
            }, separators=(',', ':'), ensure_ascii=False),
        }, signal=signal, fetcher=self.fetcher, service='banana')
