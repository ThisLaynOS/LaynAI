import asyncio
import io
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from PIL import Image

from discord import USER_AGENT
from net import AbortError, UpstreamError, request, with_signal


MAX_IMAGE_BYTES = 7_000_000
MAX_AVATAR_PIXELS = 2048 * 2048
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp'}
EXTENSIONS = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
AVATAR_HASH = r'(?:a_)?[0-9a-f]{32}'
AVATAR_PATH = re.compile(
    rf'/(?:avatars/[0-9]{{17,20}}/{AVATAR_HASH}\.(?:png|gif|webp)'
    rf'|guilds/[0-9]{{17,20}}/users/[0-9]{{17,20}}/avatars/{AVATAR_HASH}\.(?:png|gif|webp)'
    r'|embed/avatars/[0-5]\.png)'
)


class AttachmentError(ValueError):
    """A user-facing error; never includes attachment URLs or response bodies."""

    def __init__(self, message, *, code='attachment_invalid'):
        super().__init__(message)
        self.code = code


def image_attachment(event):
    """Resolve the selected avatar/photo, the current message, or its reply."""
    if event.get('type') == 2:
        data = event.get('data') or {}
        options = {o.get('name'): o.get('value') for o in data.get('options') or []}
        photo_id = options.get('foto')
        user_id = options.get('usuario') if data.get('name') == 'imagem' else None
        if user_id is not None:
            if photo_id is not None:
                raise AttachmentError('Escolha usuario para editar um avatar ou foto para editar um anexo, um por pedido.')
            return avatar_attachment(event, str(user_id))
        if photo_id is None:
            return None
        attachment = ((data.get('resolved') or {}).get('attachments') or {}).get(str(photo_id))
        if not attachment:
            raise AttachmentError('Não consegui acessar a foto. Anexe o arquivo novamente.')
        attachments = [attachment]
    else:
        attachments = event.get('attachments') or []
        ref = event.get('message_reference') or {}
        if not attachments and ref.get('channel_id', event.get('channel_id')) == event.get('channel_id'):
            attachments = (event.get('referenced_message') or {}).get('attachments') or []
    if not attachments:
        return None
    if len(attachments) != 1:
        raise AttachmentError('Envie uma foto por pedido de edição.')
    attachment = attachments[0]
    validate_image_attachment(attachment)
    return attachment


def avatar_attachment(event, user_id):
    """Request a still of the exact avatar, including assets with animated hashes."""
    resolved = (event.get('data') or {}).get('resolved') or {}
    user = (resolved.get('users') or {}).get(user_id)
    if (not re.fullmatch(r'[0-9]{17,20}', user_id) or not isinstance(user, dict)
            or str(user.get('id')) != user_id or 'avatar' not in user):
        raise AttachmentError('Não consegui acessar o avatar desse usuário. Selecione a pessoa novamente em usuario.')
    member = (resolved.get('members') or {}).get(user_id) or {}
    guild_id = event.get('guild_id')
    avatar_hash = member.get('avatar') if guild_id else None
    if avatar_hash:
        if (not re.fullmatch(r'[0-9]{17,20}', str(guild_id))
                or not isinstance(avatar_hash, str) or not re.fullmatch(AVATAR_HASH, avatar_hash)):
            raise AttachmentError('O avatar desse usuário no servidor é inválido. Selecione a pessoa novamente.')
        path = f'guilds/{guild_id}/users/{user_id}/avatars/{avatar_hash}.png'
    elif user.get('avatar'):
        avatar_hash = user['avatar']
        if not isinstance(avatar_hash, str) or not re.fullmatch(AVATAR_HASH, avatar_hash):
            raise AttachmentError('O avatar desse usuário é inválido. Selecione a pessoa novamente.')
        path = f'avatars/{user_id}/{avatar_hash}.png'
    else:
        discriminator = str(user.get('discriminator') or '0')
        if not re.fullmatch(r'[0-9]{1,4}', discriminator):
            raise AttachmentError('Não consegui identificar o avatar padrão desse usuário.')
        index = int(discriminator) % 5 if int(discriminator) else (int(user_id) >> 22) % 6
        path = f'embed/avatars/{index}.png'
    # Editing needs one frame. Downloading animated=true needlessly fetches every
    # frame and can exhaust the download/decode budget before generation starts.
    # Avatar sizes are unknown before download; the streaming byte limit still applies.
    attachment = {'source': 'avatar', 'user_id': user_id, 'filename': 'avatar.png',
                  'content_type': 'image/png', 'url': f'https://cdn.discordapp.com/{path}?size=1024'}
    validate_image_attachment(attachment)
    return attachment


def validate_image_attachment(attachment):
    is_avatar = attachment.get('source') == 'avatar'
    mime = (attachment.get('content_type') or '').split(';', 1)[0].strip().lower()
    if not mime or mime == 'application/octet-stream':
        mime = EXTENSIONS.get(PurePosixPath(attachment.get('filename') or '').suffix.lower())
    if mime not in IMAGE_TYPES and not (is_avatar and mime == 'image/gif'):
        raise AttachmentError('Envie uma foto em PNG, JPEG ou WebP para editar.')
    size = attachment.get('size')
    if not (is_avatar and size is None):
        if type(size) is not int or size <= 0:
            raise AttachmentError('O arquivo da foto está vazio ou é inválido. Anexe novamente.')
        if size > MAX_IMAGE_BYTES:
            raise AttachmentError('A foto deve ter no máximo 7 MB. Reduza o arquivo e tente novamente.')
    url = attachment.get('url') or ''
    try:
        parsed = urlsplit(url)
        valid_path = (parsed.hostname == 'cdn.discordapp.com' and AVATAR_PATH.fullmatch(parsed.path)
                      if is_avatar else
                      re.fullmatch(r'/(?:ephemeral-)?attachments/[0-9]+/[0-9]+/[^/]+', parsed.path))
        safe = (parsed.scheme == 'https'
                and parsed.hostname in ('cdn.discordapp.com', 'media.discordapp.net')
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment and not re.search(r'[\s\\]', url)
                and valid_path)
    except (TypeError, ValueError):
        safe = False
    if not safe:
        raise AttachmentError('Anexe a foto diretamente no Discord para editar.')
    return mime


def _avatar_png(raw):
    """Decode only the first frame, keeping alpha and bounding decoded/output size."""
    try:
        with Image.open(io.BytesIO(raw), formats=('PNG', 'JPEG', 'GIF', 'WEBP')) as original:
            if original.width * original.height > MAX_AVATAR_PIXELS:
                raise AttachmentError('O avatar tem resolução grande demais para editar.')
            original.seek(0)
            original.load()
            if original.format == 'PNG' and not getattr(original, 'is_animated', False):
                return {'data': raw, 'mime': 'image/png'}
            with original.convert('RGBA') as frame, io.BytesIO() as output:
                frame.info.clear()
                frame.save(output, format='PNG')
                if output.tell() > MAX_IMAGE_BYTES:
                    raise AttachmentError('O avatar convertido para PNG deve ter no máximo 7 MB.')
                return {'data': output.getvalue(), 'mime': 'image/png'}
    except AttachmentError:
        raise
    except (OSError, ValueError, EOFError, RuntimeError, KeyError, Image.DecompressionBombError) as error:
        raise AttachmentError('Não consegui converter o avatar para PNG. O arquivo pode estar inválido ou incompleto.',
                              code='avatar_decode') from error


async def _download_bytes(attachment, *, signal=None, fetcher=None, timeout=30):
    validate_image_attachment(attachment)
    try:
        # No Discord or Google credentials are sent to the attachment CDN.
        # Discord's CDN can reject the default Python-urllib User-Agent with HTTP 403.
        res = await request(attachment['url'], {
            'headers': {'user-agent': USER_AGENT},
            'max_bytes': MAX_IMAGE_BYTES,
            'timeout': timeout,
        }, signal=signal, fetcher=fetcher, service='attachment')
        chunks, size = [], 0
        async for chunk in res.body:
            if signal:
                signal.check()
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise AttachmentError('A foto deve ter no máximo 7 MB. Reduza o arquivo e tente novamente.')
            chunks.append(chunk)
    except AbortError:
        raise
    except UpstreamError as error:
        if attachment.get('source') == 'avatar':
            raise AttachmentError('Não consegui baixar o avatar desse usuário. Tente novamente ou escolha outra pessoa.',
                                  code='avatar_download') from error
        raise AttachmentError('Não consegui baixar a foto. Anexe novamente e tente de novo.') from error
    except (OSError, RuntimeError, TypeError) as error:
        if attachment.get('source') == 'avatar':
            raise AttachmentError('Não consegui ler o avatar desse usuário. Tente novamente mais tarde.',
                                  code='avatar_download') from error
        raise AttachmentError('Não consegui ler a foto. Envie um arquivo de até 7 MB novamente.') from error
    return b''.join(chunks)


async def download_image(attachment, *, signal=None, fetcher=None, timeout=30):
    mime = validate_image_attachment(attachment)
    if attachment.get('source') == 'avatar':
        sources = [attachment]
        parsed = urlsplit(attachment['url'])
        if not parsed.path.startswith('/embed/avatars/'):
            # Some original upload formats cannot be served as PNG. Static WebP
            # is a fallback for the SAME hash/user/guild, never a different avatar.
            webp_path = str(PurePosixPath(parsed.path).with_suffix('.webp'))
            fallback = {**attachment, 'filename': 'avatar.webp', 'content_type': 'image/webp',
                        'url': f'https://cdn.discordapp.com{webp_path}?size=1024'}
            if fallback['url'] != attachment['url']:
                sources.append(fallback)
        for index, source in enumerate(sources):
            try:
                raw = await _download_bytes(source, signal=signal, fetcher=fetcher, timeout=timeout)
                return await with_signal(asyncio.to_thread(_avatar_png, raw), signal)
            except AttachmentError:
                if index == len(sources) - 1:
                    raise
    raw = await _download_bytes(attachment, signal=signal, fetcher=fetcher, timeout=timeout)
    signatures = {
        'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
        'image/webp': raw.startswith(b'RIFF') and raw[8:12] == b'WEBP',
    }
    if not signatures[mime]:
        raise AttachmentError('O arquivo não corresponde a uma foto PNG, JPEG ou WebP válida.')
    return {'data': raw, 'mime': mime}
