import json
import re

from net import Blob, FormData, json_body, request

USER_AGENT = 'DiscordBot (https://discord.com, 2.0.0) LaynAI'

COLORS = {
    'response': 0xB6A4D9,
    'image': 0xB6A4D9,
    'video': 0xB6A4D9,
    'audio': 0xB6A4D9,
    'error': 0xD98888,
    'info': 0xB6A4D9,
    'status': 0x8DB5A0,
    'help': 0xB6A4D9,
}

TITLES = {
    'status': 'Seu uso por aqui',
    'help': 'Vamos conversar',
}

# 0 servidor, 1 DM com o bot, 2 canal privado (instalação no usuário).
# Comando de guild não entra em DM; estes contextos só valem no registro global.
_DM = {'dm_permission': True, 'integration_types': [0, 1], 'contexts': [0, 1, 2]}
_GUILD = {'dm_permission': False, 'integration_types': [0], 'contexts': [0]}

commands = [
    {'name': 'perguntar', 'description': 'Converse com LaynAI usando Gemini 4 Argon', 'type': 1, **_DM, 'options': [
        {'name': 'texto', 'description': 'Sua pergunta', 'type': 3, 'required': True, 'min_length': 1, 'max_length': 4000},
        {'name': 'foto', 'description': 'Foto para editar com Nano Banana (PNG, JPEG ou WebP, até 7 MB)', 'type': 11},
        {'name': 'privado', 'description': 'Só você vê a resposta (padrão: publicar no canal)', 'type': 5, 'required': False},
    ]},
    {'name': 'limpar', 'description': 'Apague seus históricos público e privado neste canal', 'type': 1, **_DM},
    {'name': 'ajuda', 'description': 'Como conversar com o bot e como os dados são usados', 'type': 1, **_DM},
    {'name': 'status', 'description': 'Veja seu uso diário e o estado do bot', 'type': 1, **_DM},
    {'name': 'cancelar', 'description': 'Interrompa seu pedido em andamento neste canal', 'type': 1, **_DM},
    {'name': 'exportar', 'description': 'Baixe seu histórico deste canal em uma resposta privada', 'type': 1, **_DM},
    {'name': 'imagem', 'description': 'Gere ou edite uma imagem com Nano Banana', 'type': 1, **_DM, 'options': [
        {'name': 'texto', 'description': 'Descreva a imagem ou a edição desejada na foto', 'type': 3, 'required': True, 'min_length': 1, 'max_length': 4000},
        {'name': 'usuario', 'description': 'Pessoa cujo avatar você quer editar (use usuario ou foto)', 'type': 6},
        {'name': 'foto', 'description': 'Foto original para editar (PNG, JPEG ou WebP, até 7 MB)', 'type': 11},
        {'name': 'formato', 'description': 'Proporção da imagem', 'type': 3, 'choices': [
            {'name': ratio, 'value': ratio} for ratio in ('1:1', '16:9', '9:16', '4:3', '3:4')]},
        {'name': 'privado', 'description': 'Só você vê a imagem', 'type': 5},
    ]},
    {'name': 'lyra', 'description': 'Crie uma música com Lyria a partir de um prompt', 'type': 1, **_DM, 'options': [
        {'name': 'prompt', 'description': 'Descreva o estilo, tema, instrumentos e voz ou peça uma música instrumental', 'type': 3, 'required': True, 'min_length': 1, 'max_length': 4000},
        {'name': 'privado', 'description': 'Só você vê e ouve a música', 'type': 5},
    ]},
    {'name': 'cor', 'description': 'Mude a cor do seu nome neste servidor', 'type': 1, **_GUILD, 'options': [
        {'name': 'hex', 'description': 'Cor em hexadecimal, ex: #FF5500', 'type': 3, 'required': True, 'min_length': 3, 'max_length': 8},
    ]},
    {'name': 'video', 'description': 'Gera um vídeo curto com Omni Flash', 'type': 1, **_DM, 'options': [
        {'name': 'texto', 'description': 'O que o vídeo deve mostrar', 'type': 3, 'required': True, 'min_length': 3, 'max_length': 2000},
        {'name': 'formato', 'description': 'Proporção', 'type': 3, 'required': False, 'choices': [
            {'name': 'paisagem 16:9', 'value': '16:9'},
            {'name': 'retrato 9:16', 'value': '9:16'},
        ]},
        {'name': 'segundos', 'description': 'Duração', 'type': 4, 'required': False, 'choices': [
            {'name': '4 segundos', 'value': 4},
            {'name': '6 segundos', 'value': 6},
            {'name': '8 segundos', 'value': 8},
        ]},
        {'name': 'privado', 'description': 'Só você vê o vídeo', 'type': 5},
    ]},
    {'name': 'canal', 'description': 'Cria um canal ou categoria (requer Gerenciar Canais)', 'type': 1, **_GUILD, 'default_member_permissions': '16', 'options': [
        {'name': 'nome', 'description': 'Nome do canal ou categoria', 'type': 3, 'required': True, 'min_length': 1, 'max_length': 100},
        {'name': 'tipo', 'description': 'Tipo', 'type': 3, 'required': False, 'choices': [
            {'name': 'texto', 'value': 'text'},
            {'name': 'voz', 'value': 'voice'},
            {'name': 'categoria', 'value': 'category'},
        ]},
        {'name': 'categoria', 'description': 'Nome ou ID da categoria pai (canais)', 'type': 3, 'required': False},
    ]},
]


def build_embed(text, *, kind='response', title=None, footer=None, fields=None, image_name=None, me=None, color=None):
    # Nome, avatar e horário já aparecem na própria mensagem do Discord.
    embed = {
        'type': 'rich',
        'color': color if color is not None else COLORS.get(kind, COLORS['response']),
    }
    heading = title if title is not None else TITLES.get(kind)
    if heading:
        embed['title'] = clip_text(heading, 256)
    if text:
        embed['description'] = clip_text(text, 4096)
    remaining = 6000 - sum(utf16_len(embed.get(key, '')) for key in ('title', 'description'))
    if footer:
        embed['footer'] = {'text': clip_text(footer, min(2048, remaining))}
        remaining -= utf16_len(embed['footer']['text'])
    if fields:
        safe_fields = []
        for field in fields[:25]:
            name = clip_text(str(field.get('name') or '\u200b'), min(256, max(0, remaining - 1)))
            remaining -= utf16_len(name)
            value = clip_text(str(field.get('value') or '\u200b'), min(1024, max(0, remaining)))
            remaining -= utf16_len(value)
            if not name or not value:
                break
            safe_fields.append({'name': name, 'value': value, 'inline': bool(field.get('inline'))})
        if safe_fields:
            embed['fields'] = safe_fields
    if image_name:
        embed['image'] = {'url': f'attachment://{image_name}'}
    return embed


def page_row(page_id, page):
    """Setas sob o embed: esquerda volta à resposta, direita abre as fontes."""
    arrows = (('⬅️', 0), ('➡️', 1))
    return [{
        'type': 1,
        'components': [
            {
                'type': 2,
                'style': 2,
                'emoji': {'name': emoji},
                'custom_id': f'pg:{page_id}:{target}',
                'disabled': page == target,
            }
            for emoji, target in arrows
        ],
    }]


class Discord:
    def __init__(self, token, fetcher=None):
        self.token = token
        self.fetcher = fetcher
        self.me = None

    async def api(self, path, method='GET', payload=None, multipart=False):
        headers = {'authorization': f'Bot {self.token}', 'user-agent': USER_AGENT}
        if not multipart:
            headers['content-type'] = 'application/json'
        body = None if payload is None else payload if multipart else json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        callback = path.endswith('/callback')
        from net import AbortSignal
        signal = AbortSignal.timeout(2500 if callback else 45000)
        try:
            res = await request(
                f'https://discord.com/api/v10{path}',
                {'method': method, 'headers': headers, 'body': body},
                service='discord', fetcher=self.fetcher,
                attempts=1 if callback else 3, signal=signal,
            )
        finally:
            signal.close()
        if res.status == 204:
            return
        return await json_body(res)

    def callback(self, i, response):
        return self.api(f"/interactions/{i['id']}/{i['token']}/callback", 'POST', response)

    async def output(self, path, method, text, extra=None, *, kind='response', image=None, video=None, audio=None, document=None, title=None, footer=None, fields=None, color=None, components=None):
        base = {'allowed_mentions': {'parse': [], 'replied_user': False}, **(extra or {})}
        image_bytes, image_name, mime = _file_parts(image, default_name='imagem.png', default_mime='image/png')
        video_bytes, video_name, vmime = _file_parts(video, default_name='video.mp4', default_mime='video/mp4')
        audio_bytes, audio_name, amime = _file_parts(audio, default_name='musica.mp3', default_mime='audio/mpeg')
        doc_bytes, doc_name, dmime = _file_parts(document, default_name='historico.json', default_mime='application/json')
        media_heading = clip_text(title, 256) if title else ''
        text_limit = min(1800, 2000 - utf16_len(media_heading) - 5) if video_bytes or audio_bytes else 4096
        long_text = isinstance(text, str) and utf16_len(text) > text_limit
        display = text_preview(text, text_limit) if long_text else text
        embed = build_embed(display, kind=kind, title=title, footer=footer, fields=fields, image_name=image_name, me=self.me, color=color)
        payload = {**base, 'embeds': [embed], 'content': ''}
        # Discord does not render attached MP4/MP3 inside an embed (unlike images).
        # A separate embed would sit under/over the player — send caption as content instead.
        if (video_bytes or audio_bytes) and not image_bytes:
            cap = (display or '').strip()
            lines = [f'**{media_heading}**'] if media_heading else []
            if cap and cap != media_heading:
                lines.append(cap)
            if footer:
                remaining = 2000 - utf16_len('\n'.join(lines)) - 4
                foot = clip_text(footer, max(0, remaining))
                if foot:
                    lines.append(f'-# {foot}')
            payload = {**base, 'content': '\n'.join(lines), 'embeds': []}
        if components:
            payload['components'] = components
        if not image_bytes and not video_bytes and not audio_bytes and doc_bytes is None and not long_text:
            return await self.api(path, method, payload)
        form = FormData()
        attachments = []
        files = []
        idx = 0
        if long_text:
            # A generated source file can itself be named resposta.txt.
            used_names = {str(name).casefold() for name in (image_name, video_name, audio_name, doc_name) if name}
            full_name = 'resposta.txt'
            suffix = 1
            while full_name.casefold() in used_names:
                full_name = f'resposta-completa{suffix if suffix > 1 else ""}.txt'
                suffix += 1
            attachments.append({'id': idx, 'filename': full_name})
            files.append((f'files[{idx}]', Blob([text], type='text/plain;charset=utf-8'), full_name))
            idx += 1
            note = f'O texto completo está no arquivo `{full_name}`.'
            caption = payload['content']
            payload['content'] = f'{clip_text(caption, 2000 - utf16_len(note) - 2)}\n\n{note}' if caption else note
        if image_bytes:
            attachments.append({'id': idx, 'filename': image_name})
            files.append((f'files[{idx}]', Blob([image_bytes], type=mime), image_name))
            idx += 1
        if video_bytes:
            attachments.append({'id': idx, 'filename': video_name})
            files.append((f'files[{idx}]', Blob([video_bytes], type=vmime), video_name))
            idx += 1
        if audio_bytes:
            attachments.append({'id': idx, 'filename': audio_name})
            files.append((f'files[{idx}]', Blob([audio_bytes], type=amime), audio_name))
            idx += 1
        if doc_bytes is not None:
            attachments.append({'id': idx, 'filename': doc_name})
            files.append((f'files[{idx}]', Blob([doc_bytes], type=dmime), doc_name))
        payload['attachments'] = attachments
        form.set('payload_json', json.dumps(payload, separators=(',', ':'), ensure_ascii=False))
        for name, blob, filename in files:
            form.set(name, blob, filename)
        return await self.api(path, method, form, True)

    def edit(self, i, text, **kwargs):
        return self.output(f"/webhooks/{i['application_id']}/{i['token']}/messages/@original", 'PATCH', text, **kwargs)

    def reply(self, message, text, **kwargs):
        return self.output(
            f"/channels/{message['channel_id']}/messages",
            'POST',
            text,
            {'message_reference': {'message_id': message['id'], 'fail_if_not_exists': False}},
            **kwargs,
        )


def _file_parts(file, *, default_name, default_mime):
    if not file:
        return None, None, None
    if isinstance(file, (bytes, bytearray)):
        return bytes(file), default_name, default_mime
    data = file.get('data') if isinstance(file, dict) else getattr(file, 'data', None)
    mime = (file.get('mime') if isinstance(file, dict) else getattr(file, 'mime', None)) or default_mime
    name = (file.get('filename') if isinstance(file, dict) else getattr(file, 'filename', None)) or default_name
    return data, name, mime


def _image_parts(image):
    return _file_parts(image, default_name='imagem.png', default_mime='image/png')


def utf16_len(text):
    return len(text.encode('utf-16-le')) // 2


def clip_text(text, limit):
    return text.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore')


def text_preview(text, limit=4096):
    """Prefer complete paragraphs/lines while keeping Discord's UTF-16 budget."""
    if utf16_len(text) <= limit:
        return text
    preview = clip_text(text, max(0, limit - 100))
    for separator in ('\n\n', '\n', ' '):
        cut = preview.rfind(separator)
        if cut >= len(preview) // 2:
            preview = preview[:cut]
            break
    # Close a terminal/log fence if the visible preview ends inside one.
    opened = None
    for line in preview.splitlines():
        fence = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if not fence:
            continue
        marker, rest = fence.groups()
        if opened is None:
            opened = marker
        elif marker[0] == opened[0] and len(marker) >= len(opened) and not rest.strip():
            opened = None
    if opened and len(opened) <= 90:
        return preview.rstrip() + '\n' + opened + '\n\n…'
    return preview.rstrip() + '\n\n…'
