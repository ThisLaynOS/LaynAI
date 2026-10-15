import asyncio
import json
import re

from clock import clock_line
from attachments import AttachmentError
from banana import ImageGenerationError
from guild import _user_id, color_role_name, is_color_role, parse_hex
from net import AbortError, UpstreamError
from vertex import validate_arguments
from weakref import WeakValueDictionary

# Serialize structural/color changes per guild across users within this process.
_GUILD_LOCKS = WeakValueDictionary()

CHANNEL_TYPES = {
    'text': 0,
    'voice': 2,
    'category': 4,
    'announcement': 5,
    'forum': 15,
}

TYPE_NAMES = {v: k for k, v in CHANNEL_TYPES.items()}
MAX_MUTATIONS = 12
CREATE_VERBS = re.compile(r'\b(cria|crie|criar|adiciona|adicione|abre|abra|faz|fa[cç]a|fazer|monte|montar|novo|nova|novos|novas)\b', re.I)


def parse_channel_requests(prompt):
    text = (prompt or '').strip()
    if not text:
        return []
    out = []
    for match in re.finditer(r'categorias?\s+(?:chamad[aos]*\s+)?["“]?([^,"”.;\n]+)', text, re.I):
        name = match.group(1).strip()
        if name:
            out.append({'kind': 'category', 'name': name})
    blob = text
    found = re.search(r'\bcanais?\b(?:\s+(?:chamad[aos]*|de nome|:))?\s*(.+)$', text, re.I)
    if found:
        blob = re.split(r'[.!?\n]', found.group(1), maxsplit=1)[0]
    elif re.search(r'\bcanal\b', text, re.I) and CREATE_VERBS.search(text):
        found = re.search(r'\bcanal\b\s+(?:de\s+)?(?:voz\s+)?(?:chamad[ao]\s+)?(.+)$', text, re.I)
        blob = found.group(1) if found else ''
    else:
        blob = ''
    parts = [p.strip().strip('#"“”\'') for p in re.split(r'\s*,\s*|\s+e\s+', blob) if p.strip()]
    for part in parts:
        part = re.sub(r'^(um|uma|o|a|os|as|de)\s+', '', part, flags=re.I).strip()
        if not part or len(part) > 80:
            continue
        if re.search(r'\bcategoria\b', part, re.I):
            continue
        voice = bool(re.search(r'\bvoz\b|voice', part, re.I))
        name = re.sub(r'\b(canal(?:s)?(?:\s+de)?|de\s+voz|voz)\b', '', part, flags=re.I).strip() or ('voz' if voice else part)
        name = re.sub(r'\s+', ' ', name).strip(' -_')
        if not name:
            continue
        out.append({'kind': 'voice' if voice else 'text', 'name': name})
    # de-dupe by name
    seen = set()
    uniq = []
    for item in out:
        key = (item['kind'], item['name'].lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


class Actions:
    def __init__(self, discord, banana=None, log=lambda *a, **k: None, omni=None, search=None):
        self.discord = discord
        self.banana = banana
        self.omni = omni
        self.search = search
        self.log = log
        self.mutations = 0

    async def run(self, name, args, ctx, admin=None):
        args = {} if args is None else args
        ctx = {} if ctx is None else ctx
        error = validate_arguments(name, args)
        if error:
            return {'ok': False, 'error': error}
        if ctx.get('signal'):
            ctx['signal'].check()
        if admin is not None:
            ctx['admin'] = admin
        if not ctx.get('user_id'):
            ctx['user_id'] = _user_id(ctx)
        guild = ctx.get('guild_id')
        if guild and name not in ('generate_image', 'generate_video', 'web_search'):
            lock = _GUILD_LOCKS.setdefault(guild, asyncio.Lock())
            async with lock:
                ctx.pop('_channels', None)
                return await self._run(name, args, ctx)
        return await self._run(name, args, ctx)

    async def _run(self, name, args, ctx):
        if ctx.get('signal'):
            ctx['signal'].check()
        try:
            if name == 'generate_image':
                return await self.generate_image(args, ctx)
            if name == 'generate_video':
                return await self.generate_video(args, ctx)
            if name == 'web_search':
                return await self.web_search(args, ctx)
            if name == 'set_my_color':
                return await self.set_my_color(args, ctx)
            if name in ('list_server', 'create_category', 'create_channel', 'create_channels', 'edit_channel', 'apply_channel_names'):
                if not ctx.get('admin'):
                    return {'ok': False, 'error': 'Só administradores do servidor podem alterar a estrutura.'}
                if name == 'list_server':
                    return await self.list_server(ctx)
                if self.mutations >= MAX_MUTATIONS:
                    return {'ok': False, 'error': f'Limite de {MAX_MUTATIONS} alterações neste pedido.'}
                if name == 'create_category':
                    return await self.create_category(args, ctx)
                if name == 'create_channel':
                    return await self.create_channel(args, ctx)
                if name == 'create_channels':
                    return await self.create_channels(args, ctx)
                if name == 'apply_channel_names':
                    return await self.apply_channel_names(args, ctx)
                return await self.edit_channel(args, ctx)
            return {'ok': False, 'error': f'Ferramenta desconhecida: {name}'}
        except UpstreamError as e:
            return _discord_error(e, 'completar a ação no Discord')

    async def generate_image(self, args, ctx):
        if ctx.get('image') or ctx.get('video'):
            return {'ok': False, 'error': 'Gere uma imagem ou um vídeo por pedido.'}
        if not self.banana:
            return {'ok': False, 'error': 'Geração de imagem indisponível.'}
        prompt = (args.get('prompt') or '').strip()
        if not prompt:
            return {'ok': False, 'error': 'Informe o prompt da imagem.'}
        attachment = ctx.get('image_attachment')
        options = {'attachment': attachment} if attachment else {}
        aspect = args.get('aspect_ratio') or (None if attachment else '1:1')
        try:
            image = await self.banana.generate(prompt, aspect, ctx.get('signal'), **options)
        except (AttachmentError, ImageGenerationError) as error:
            self.log('image_failed', code=error.code, error=type(error).__name__, status=getattr(error, 'status', 0))
            return {'ok': False, 'error': str(error)}
        ctx['image'] = image
        caption = (args.get('caption') or '').strip() or getattr(image, 'caption', '') or 'Aqui está a imagem.'
        if attachment and attachment.get('source') == 'avatar':
            caption = f"Aqui está a versão editada do avatar de <@{attachment['user_id']}>.\n\n{caption}"
        ctx['image_caption'] = caption
        return {'ok': True, 'caption': caption}

    async def generate_video(self, args, ctx):
        if ctx.get('image') or ctx.get('video'):
            return {'ok': False, 'error': 'Gere uma imagem ou um vídeo por pedido.'}
        if not self.omni:
            return {'ok': False, 'error': 'Geração de vídeo indisponível.'}
        prompt = (args.get('prompt') or '').strip()
        if not prompt:
            return {'ok': False, 'error': 'Descreva o vídeo.'}
        from net import AbortSignal
        signal = ctx.get('signal') or AbortSignal.timeout(180000)
        try:
            seconds = int(args.get('seconds') or args.get('duration') or 6)
        except (TypeError, ValueError):
            seconds = 6
        video = await self.omni.generate(prompt, args.get('aspect_ratio') or '16:9', seconds, signal)
        ctx['video'] = video
        caption = (args.get('caption') or '').strip() or getattr(video, 'caption', '') or 'Aqui está o vídeo.'
        ctx['video_caption'] = caption
        return {'ok': True, 'caption': caption}

    async def web_search(self, args, ctx):
        if not self.search:
            return {'ok': False, 'error': 'Pesquisa indisponível.'}
        query = (args.get('query') or '').strip()
        if not query:
            return {'ok': False, 'error': 'Informe o que pesquisar.'}
        if len(query) > 300:
            return {'ok': False, 'error': 'Consulta longa demais. Use até 300 caracteres.'}
        try:
            found = await self.search.search(query, ctx.get('signal'))
        except AbortError:
            raise
        except UpstreamError as err:
            if err.status == 429:
                return {'ok': False, 'error': 'A pesquisa do Google está no limite de uso. Tente mais tarde.'}
            if err.status in (401, 403):
                return {'ok': False, 'error': 'A pesquisa Google não está liberada neste projeto.'}
            return {'ok': False, 'error': 'A pesquisa falhou.'}
        except RuntimeError as err:
            message = str(err)
            if message.startswith('A pesquisa'):
                return {'ok': False, 'error': message}
            return {'ok': False, 'error': 'A pesquisa não retornou um resultado utilizável.'}
        except (TimeoutError, json.JSONDecodeError):
            return {'ok': False, 'error': 'A pesquisa não retornou um resultado utilizável.'}
        if not isinstance(found, dict):
            return {'ok': False, 'error': 'A pesquisa não retornou um resultado utilizável.'}
        return {
            'ok': True,
            'query': found.get('query') or query,
            'answer': found.get('answer') or '',
            'sources': found.get('sources') or [],
            'searches': found.get('searches') or [query],
            'as_of': clock_line(),
        }

    async def set_my_color(self, args, ctx):
        parsed = parse_hex(args.get('hex') or args.get('color') or '')
        if not parsed:
            return {'ok': False, 'error': 'Use uma cor hexadecimal, por exemplo #FF5500.'}
        raw, color = parsed
        hex_code = f'#{raw.upper()}'
        guild_id, user_id = ctx.get('guild_id'), ctx.get('user_id')
        if not guild_id or not user_id:
            return {'ok': False, 'error': 'Cores só funcionam em um servidor, não em mensagem direta.'}
        # Never honor a target user supplied by the model.
        try:
            member = await self.discord.api(f'/guilds/{guild_id}/members/{user_id}')
            roles = await self.discord.api(f'/guilds/{guild_id}/roles')
        except UpstreamError as e:
            return _discord_error(e, 'ler cargos')
        owned = [
            r for r in (roles or [])
            if is_color_role(r) and r.get('id') in set((member or {}).get('roles') or [])
        ]
        name = color_role_name(hex_code)
        try:
            # Reuse immutable color roles. Editing an existing role recolors every holder.
            role = next((r for r in (roles or []) if is_color_role(r) and r.get('name') == name and r.get('color') == color), None)
            if role is None:
                role = await self.discord.api(f'/guilds/{guild_id}/roles', 'POST', {
                    'name': name,
                    'color': color,
                    'hoist': False,
                    'mentionable': False,
                    'permissions': '0',
                })
            await self.discord.api(
                f"/guilds/{guild_id}/members/{user_id}/roles/{role['id']}", 'PUT',
            )
            for previous in owned:
                if previous['id'] != role['id']:
                    await self.discord.api(f"/guilds/{guild_id}/members/{user_id}/roles/{previous['id']}", 'DELETE')
        except UpstreamError as e:
            return _discord_error(e, 'alterar a cor do perfil')
        ctx['color'] = color
        ctx['color_hex'] = hex_code
        self.log('color_set')
        return {'ok': True, 'hex': hex_code, 'role': name}

    async def list_server(self, ctx):
        error = self._guard(ctx, mutation=False)
        if error:
            return error
        guild_id = ctx.get('guild_id')
        try:
            channels = await self.discord.api(f'/guilds/{guild_id}/channels')
        except UpstreamError as e:
            return _discord_error(e, 'listar canais')
        ctx['_channels'] = channels or []
        out = []
        for ch in channels or []:
            out.append({
                'id': ch.get('id'),
                'name': _display_name(ch.get('name')),
                'type': TYPE_NAMES.get(ch.get('type'), ch.get('type')),
                'parent_id': ch.get('parent_id'),
            })
        return {'ok': True, 'channels': out[:200]}

    async def create_category(self, args, ctx):
        error = self._guard(ctx)
        if error:
            return error
        name = _channel_name(args.get('name'), kind='category')
        if not name:
            return {'ok': False, 'error': 'Informe o nome da categoria.'}
        existing = await self._find_existing(name, 'category', ctx)
        if existing:
            return {'ok': True, 'id': existing.get('id'), 'name': existing.get('name'), 'type': 'category', 'existed': True}
        try:
            created = await self.discord.api(f"/guilds/{ctx['guild_id']}/channels", 'POST', {
                'name': name,
                'type': CHANNEL_TYPES['category'],
            })
        except UpstreamError as e:
            return _discord_error(e, 'criar categoria')
        ctx.pop('_channels', None)
        self.mutations += 1
        self.log('channel_created', kind='category')
        await _pace()
        return {'ok': True, 'id': created.get('id'), 'name': _display_name(created.get('name')), 'type': 'category'}

    async def create_channel(self, args, ctx):
        error = self._guard(ctx)
        if error:
            return error
        kind = (args.get('type') or 'text').lower()
        if kind not in CHANNEL_TYPES or kind == 'category':
            return {'ok': False, 'error': 'Tipo inválido. Use text, voice, announcement ou forum.'}
        name = _channel_name(args.get('name'), kind=kind)
        if not name:
            return {'ok': False, 'error': 'Informe o nome do canal.'}
        payload = {'name': name, 'type': CHANNEL_TYPES[kind]}
        parent = await self._resolve_parent(args.get('category') or args.get('parent_id'), ctx)
        if parent is False:
            return {'ok': False, 'error': 'Categoria não encontrada.'}
        if parent:
            payload['parent_id'] = parent
        existing = await self._find_existing(name, kind, ctx, parent=parent)
        if existing:
            return {'ok': True, 'id': existing.get('id'), 'name': existing.get('name'), 'type': kind, 'existed': True}
        topic = (args.get('topic') or '').strip()
        if topic and kind in ('text', 'announcement', 'forum'):
            payload['topic'] = topic[:1024]
        try:
            created = await self.discord.api(f"/guilds/{ctx['guild_id']}/channels", 'POST', payload)
        except UpstreamError as e:
            return _discord_error(e, 'criar canal')
        ctx.pop('_channels', None)
        self.mutations += 1
        self.log('channel_created', kind=kind)
        await _pace()
        return {'ok': True, 'id': created.get('id'), 'name': _display_name(created.get('name')), 'type': kind}

    async def edit_channel(self, args, ctx):
        error = self._guard(ctx)
        if error:
            return error
        lookup = args.get('channel_id') or args.get('channel') or args.get('channel_name')
        new_name = args.get('new_name')
        if args.get('name') and lookup:
            new_name = new_name or args.get('name')
        elif args.get('name') and not lookup:
            lookup = args.get('name')
        channel_id = await self._resolve_channel(lookup, ctx)
        if not channel_id:
            names = await self._channel_names(ctx)
            return {
                'ok': False,
                'error': f'Canal não encontrado: {lookup!r}. Canais atuais: {", ".join(names[:25])}',
            }
        payload = {}
        if new_name:
            kind = 'text'
            for ch in await self._guild_channels(ctx):
                if ch.get('id') == channel_id:
                    kind = TYPE_NAMES.get(ch.get('type'), 'text')
                    break
            payload['name'] = _channel_name(new_name, kind=kind) or new_name
        if args.get('topic') is not None:
            payload['topic'] = str(args.get('topic'))[:1024]
        if args.get('nsfw') is not None:
            payload['nsfw'] = bool(args.get('nsfw'))
        if args.get('slowmode') is not None or args.get('rate_limit_per_user') is not None:
            try:
                payload['rate_limit_per_user'] = max(0, min(21600, int(args.get('slowmode') or args.get('rate_limit_per_user') or 0)))
            except (TypeError, ValueError):
                pass
        parent = args.get('category') or args.get('category_id') or args.get('parent_id')
        if parent:
            resolved = await self._resolve_parent(parent, ctx)
            if resolved is False:
                return {'ok': False, 'error': 'Categoria não encontrada.'}
            payload['parent_id'] = resolved
        if not payload:
            return {'ok': False, 'error': 'Nada para alterar (envie name, topic, category, nsfw ou slowmode).'}
        try:
            edited = await self.discord.api(f'/channels/{channel_id}', 'PATCH', payload)
        except UpstreamError as e:
            return _discord_error(e, 'editar canal')
        ctx.pop('_channels', None)
        self.mutations += 1
        self.log('channel_edited', fields=','.join(payload.keys()))
        await _pace()
        return {
            'ok': True,
            'id': edited.get('id'),
            'name': _display_name(edited.get('name')),
            'changed': list(payload.keys()),
            'topic': edited.get('topic'),
        }

    async def create_channels(self, args, ctx):
        items = args.get('channels') if isinstance(args.get('channels'), list) else []
        if not items:
            return {'ok': False, 'error': 'Envie channels: [{name, type, category}].'}
        results = []
        for item in items[:MAX_MUTATIONS]:
            if not isinstance(item, dict):
                continue
            if self.mutations >= MAX_MUTATIONS:
                results.append({'ok': False, 'error': f'Limite de {MAX_MUTATIONS} alterações neste pedido.'})
                break
            results.append(await self.create_channel(item, ctx))
        done = sum(1 for r in results if r.get('ok'))
        return {'ok': done > 0, 'done': done, 'total': len(results), 'results': results}

    async def apply_channel_names(self, args, ctx):
        items = args.get('renames') or args.get('channels')
        if not isinstance(items, list) or not items:
            return {'ok': False, 'error': 'Envie renames: [{channel, name}].'}
        results = []
        for item in items[:MAX_MUTATIONS]:
            if not isinstance(item, dict):
                continue
            if self.mutations >= MAX_MUTATIONS:
                results.append({'ok': False, 'error': f'Limite de {MAX_MUTATIONS} alterações neste pedido.'})
                break
            results.append(await self.edit_channel({
                'channel_id': item.get('channel') or item.get('channel_id') or item.get('from'),
                'name': item.get('name') or item.get('to'),
                'topic': item.get('topic'),
                'category': item.get('category') or item.get('category_id'),
            }, ctx))
        done = sum(1 for r in results if r.get('ok'))
        return {'ok': done > 0, 'done': done, 'total': len(results), 'results': results}

    def _guard(self, ctx, mutation=True):
        if ctx.get('signal'):
            ctx['signal'].check()
        if not ctx.get('admin') or not ctx.get('guild_id'):
            return {'ok': False, 'error': 'Só administradores ou membros com Gerenciar Canais podem alterar a estrutura.'}
        if mutation and self.mutations >= MAX_MUTATIONS:
            return {'ok': False, 'error': f'Limite de {MAX_MUTATIONS} alterações neste pedido.'}
        return None

    async def _find_existing(self, name, kind, ctx, parent=None):
        want = CHANNEL_TYPES.get(kind)
        slug = _norm_chan(name)
        for ch in await self._guild_channels(ctx):
            if want is not None and ch.get('type') != want:
                continue
            if kind != 'category' and (ch.get('parent_id') or None) != parent:
                continue
            if _norm_chan(ch.get('name')) == slug:
                return ch
        return None

    async def _resolve_parent(self, value, ctx):
        if not value:
            return None
        return await self._resolve_channel(value, ctx, want_category=True)

    async def _guild_channels(self, ctx):
        if ctx.get('_channels') is None:
            ctx['_channels'] = await self.discord.api(f"/guilds/{ctx['guild_id']}/channels") or []
        return ctx.get('_channels') or []

    async def _channel_names(self, ctx):
        return [_display_name(c.get('name')) for c in await self._guild_channels(ctx) if c.get('name')]

    async def _resolve_channel(self, value, ctx, want_category=False):
        if not value:
            return None
        text = str(value).strip()
        channels = await self._guild_channels(ctx)
        pool = []
        for ch in channels:
            if want_category and ch.get('type') != CHANNEL_TYPES['category']:
                continue
            pool.append(ch)
        if re.fullmatch(r'\d{17,20}', text):
            return text if any(str(ch.get('id')) == text for ch in pool) else False
        needle = text.lower().lstrip('#')
        slug = _norm_chan(text)
        exact = [c for c in pool if (c.get('name') or '').lower() == needle]
        if len(exact) == 1:
            return exact[0].get('id')
        slugged = [c for c in pool if _norm_chan(c.get('name')) == slug]
        if len(slugged) == 1:
            return slugged[0].get('id')
        partial = [c for c in pool if slug and slug in _norm_chan(c.get('name'))]
        if len(partial) == 1:
            return partial[0].get('id')
        return False if want_category or text else None


async def _pace():
    await asyncio.sleep(0.55)


def _display_name(name):
    return (name or '').replace('\u2800', ' ')


def _norm_chan(name):
    text = (name or '').lower().lstrip('#').replace('|', '│').replace('\u2800', ' ')
    text = re.sub(r'[\W_]+', '-', text, flags=re.UNICODE)
    return text.strip('-')


# Discord's bot API still turns ASCII spaces into "-" on text channels.
# Braille blank (U+2800) looks like a space and survives that sanitizer.
_BLANK = '\u2800'


def _channel_name(name, *, kind='text', category=False):
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text:
        return None
    text = text.replace('|', '│')
    text = re.sub(r'[#,]', '', text)
    # Undo repeated hyphens after Discord sanitizes spaces.
    text = re.sub(r'\s*-\s*│\s*-\s*', ' │ ', text)
    text = re.sub(r'\s+│\s+', ' │ ', text)
    text = re.sub(r' +', ' ', text).strip()
    if category or kind in ('category', 'voice', 'stage'):
        return text[:100] or None
    return text.replace(' ', _BLANK)[:100] or None


def _discord_error(err, action):
    if getattr(err, 'status', None) == 403:
        return {
            'ok': False,
            'error': (
                f'Sem permissão para {action}. No Discord, dê ao bot Gerenciar Cargos e Gerenciar Canais, '
                'e deixe o cargo do bot acima das tags de cor.'
            ),
        }
    if getattr(err, 'status', None) == 429:
        return {'ok': False, 'error': 'O Discord limitou as alterações. Espere um pouco e tente de novo.', 'rate_limited': True}
    if getattr(err, 'status', None) == 400:
        return {'ok': False, 'error': f'O Discord recusou o pedido ao {action}. Confira o nome do canal.'}
    return {'ok': False, 'error': f'Falha ao {action}.'}
