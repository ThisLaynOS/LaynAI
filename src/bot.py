import asyncio
import json
import re
from copy import copy
from urllib.parse import quote_plus

from actions import Actions
from attachments import AttachmentError, image_attachment
from discord import build_embed, page_row, text_preview
from guild import event_is_admin
from net import AbortError, AbortSignal
from lyria import MusicError
from presentation import prepare_response
from store import context
from vertex import TOOLS
from contextlib import suppress

help_fields = [
    {'name': 'Pra conversar', 'value': 'Me marque no canal, mande uma DM ou use `/perguntar`. No canal, todos veem a resposta; com `privado:true`, só você vê.', 'inline': False},
    {'name': 'Código em arquivo', 'value': 'Peça um script ou projeto normalmente. O código vem em um arquivo para baixar; vários arquivos vêm em ZIP com as pastas e as instruções. Comandos curtos continuam na mensagem para copiar.', 'inline': False},
    {'name': 'Pesquisa', 'value': 'Pergunte sobre notícias ou fatos atuais. A resposta vem sem os links; a seta da direita abre as fontes. A data e a hora eu já sei.', 'inline': False},
    {'name': 'Imagens e vídeos', 'value': 'Use `/imagem` ou `/video`. Para editar, anexe a `foto` no `/imagem` ou `/perguntar`, ou envie foto + pedido na DM ou me marcando no canal. Também pode responder à mensagem da foto e me marcar. Aceito uma foto PNG, JPEG ou WebP de até 7 MB. Os comandos aceitam `privado:true`.', 'inline': False},
    {'name': 'Editar um avatar', 'value': 'Use `/imagem usuario:@pessoa texto:coloque óculos escuros e um fundo azul`. Eu busco o avatar da pessoa e devolvo a versão editada. Você pode usar `privado:true`. Escolha `usuario` ou `foto` em cada pedido.', 'inline': False},
    {'name': 'Criar música', 'value': 'Use `/lyra prompt:pop animado em português sobre uma viagem com amigos`. Descreva o estilo, tema e voz, ou peça uma música instrumental. Eu devolvo um MP3. Aceita `privado:true` e `/cancelar`.', 'inline': False},
    {'name': 'Sua cor', 'value': 'Use `/cor hex:#FF5500` pra mudar a cor do seu nome neste servidor.', 'inline': False},
    {'name': 'Organizar o servidor', 'value': 'Use `/canal` pra criar canais ou categorias, ou me peça em texto pra criar e renomear canais. Você precisa de Administrador ou Gerenciar Canais.', 'inline': False},
    {'name': 'Seu histórico', 'value': '`/limpar` apaga o que guardei das suas conversas públicas e privadas neste canal. As mensagens no Discord continuam lá. O histórico fica separado por pessoa, canal e visibilidade.', 'inline': False},
    {'name': 'Parar ou salvar', 'value': '`/cancelar` interrompe seu pedido atual. `/exportar` te entrega um arquivo com seu histórico deste canal, só pra você.', 'inline': False},
    {'name': 'Seu uso', 'value': '`/status` mostra quantos pedidos você já fez hoje e o que está disponível.', 'inline': False},
]

SIDE_EFFECT_TOOLS = {'generate_image', 'generate_video', 'set_my_color'}
SERVER_TOOLS = {'list_server', 'create_category', 'create_channel', 'create_channels', 'edit_channel', 'apply_channel_names'}


def identity(i):
    member = i.get('member') if isinstance(i, dict) else None
    user = (member or {}).get('user') if member else None
    if user and user.get('id'):
        return user['id']
    if i.get('user') and i['user'].get('id'):
        return i['user']['id']
    if i.get('author') and i['author'].get('id'):
        return i['author']['id']
    return None


def scope(i):
    return f"{i.get('guild_id') or 'dm'}:{i.get('channel_id')}:{identity(i)}"


def mentionPrompt(content, bot_id):
    if not isinstance(content, str):
        return None
    pattern = re.compile(rf'<@!?{re.escape(str(bot_id))}>')
    if not pattern.search(content):
        return None
    return pattern.sub('', content).strip()


def is_addressed_to_bot(m, bot_id):
    bot_id = str(bot_id)
    if mentionPrompt(m.get('content'), bot_id) is not None:
        return True
    for user in m.get('mentions') or []:
        if str(user.get('id') or '') == bot_id:
            return True
    author = ((m.get('referenced_message') or {}).get('author') or {})
    return str(author.get('id') or '') == bot_id


def addressed_prompt(m, bot_id):
    if not is_addressed_to_bot(m, bot_id):
        return None
    mentioned = mentionPrompt(m.get('content'), bot_id)
    if mentioned is not None:
        return mentioned
    return (m.get('content') or '').strip()


def immediate(text, *, kind='info', title=None, fields=None, me=None, color=None):
    return {
        'type': 4,
        'data': {
            'flags': 64,
            'allowed_mentions': {'parse': []},
            'embeds': [build_embed(text, kind=kind, title=title, fields=fields, me=me, color=color)],
        },
    }


def response_text(payload):
    data = payload.get('data') or {}
    if data.get('content'):
        return data['content']
    embeds = data.get('embeds') or []
    if embeds:
        return embeds[0].get('description') or ''
    return ''


class Bot:
    def __init__(self, cfg, store, vertex, discord, log=lambda *a, **k: None, banana=None, omni=None, search=None, lyria=None):
        self.cfg = cfg
        self.store = store
        self.vertex = vertex
        self.discord = discord
        self.banana = banana if cfg.enableImages else None
        self.omni = omni if cfg.enableVideos else None
        self.search = search if cfg.enableSearch else None
        self.lyria = lyria if cfg.enableMusic else None
        self.log = log
        self.active = set()
        self.jobs = set()
        self.closing = False
        self.running = {}
        self.started = _now()

    def authorized(self, i):
        user = identity(i)
        return bool(user) and bool(i.get('channel_id')) and \
            (not self.cfg.allowUsers or user in self.cfg.allowUsers) and \
            (not self.cfg.allowGuilds or i.get('guild_id') in self.cfg.allowGuilds) and \
            (not self.cfg.allowChannels or i.get('channel_id') in self.cfg.allowChannels)

    async def interaction(self, i, respond):
        me = getattr(self.discord, 'me', None)
        if i.get('type') == 1:
            await respond({'type': 1})
            return
        if i.get('type') == 3:
            await self.turn_page(i, respond)
            return
        if i.get('type') != 2:
            await respond(immediate('Interação não suportada.', kind='error', me=me))
            return
        if not self.authorized(i):
            await respond(immediate('Este usuário, servidor ou canal não está autorizado.', kind='error', me=me))
            return
        if not self.store.claim(f"i:{i.get('id')}"):
            await respond(immediate('Esta interação já foi recebida.', kind='error', me=me))
            return
        key = scope(i)
        name = (i.get('data') or {}).get('name')
        if name == 'ajuda':
            await respond(immediate(
                'Pode falar comigo do seu jeito. Aqui vão alguns atalhos:',
                kind='help', fields=help_fields, me=me,
            ))
            return
        if name == 'status':
            await respond(immediate(
                'Aqui está seu uso de hoje. Os limites diários reiniciam à meia-noite (UTC).',
                kind='status',
                fields=[
                    {'name': 'Seus pedidos', 'value': f'{self.store.count(identity(i))} de {self.cfg.userDaily} hoje', 'inline': True},
                    {'name': 'Pedidos em andamento', 'value': f'{len(self.active)} de {self.cfg.maxConcurrent} vagas em uso', 'inline': True},
                    {'name': 'Imagens', 'value': 'Disponíveis' if self.banana else 'Indisponíveis no momento', 'inline': True},
                    {'name': 'Vídeos', 'value': 'Disponíveis' if self.omni else 'Indisponíveis no momento', 'inline': True},
                    {'name': 'Músicas', 'value': 'Disponíveis' if self.lyria else 'Indisponíveis no momento', 'inline': True},
                    {'name': 'Pesquisa', 'value': 'Disponível' if self.search else 'Indisponível no momento', 'inline': True},
                    {'name': 'Tokens de texto usados hoje', 'value': str(self.store.tokens(identity(i))['total_tokens']), 'inline': True},
                    {'name': 'Tempo online', 'value': f'{(_now() - self.started) // 60000} min', 'inline': True},
                ],
                me=me,
            ))
            return
        if name == 'cancelar':
            task = self.running.get(key)
            if task and not task.done():
                task.cancel()
                message = 'Pedi pra interromper seu pedido. Isso não desfaz o que já foi concluído nem cancela cobranças do serviço.'
            else:
                message = 'Você não tem um pedido em andamento neste canal.'
            await respond(immediate(message, me=me))
            return
        if name == 'exportar':
            await respond({'type': 5, 'data': {'flags': 64}})
            self._start_job(self._export_job(i))
            return
        if name in ('cor', 'canal', 'imagem'):
            opts = {o['name']: o['value'] for o in (i.get('data') or {}).get('options') or []}
            prompt = opts.get('texto') if name == 'imagem' else opts.get('nome') if name == 'canal' else opts.get('hex')
            error = self.reserve(i, prompt)
            if error:
                await respond(immediate(error, kind='error', me=me))
                return
            private = name != 'imagem' or opts.get('privado') is True
            try:
                await respond({'type': 5, 'data': {'flags': 64 if private else 0}})
            except Exception:
                self.active.discard(key)
                raise
            self._start_job(self._action_job(i, name, opts), key)
            return
        if name == 'lyra':
            if not self.lyria:
                await respond(immediate('Geração de música indisponível no momento.', kind='error', me=me))
                return
            opts = {o['name']: o['value'] for o in (i.get('data') or {}).get('options') or []}
            prompt = opts.get('prompt')
            error = self.reserve(i, prompt)
            if error:
                await respond(immediate(error, kind='error', me=me))
                return
            try:
                await respond({'type': 5, 'data': {'flags': 64 if opts.get('privado') is True else 0}})
            except Exception:
                self.active.discard(key)
                raise
            self._start_job(self._music_job(i, prompt), key)
            return
        if name == 'video':
            opts = {o['name']: o['value'] for o in (i.get('data') or {}).get('options') or []}
            prompt = opts.get('texto')
            error = self.reserve(i, prompt)
            if error:
                await respond(immediate(error, kind='error', me=me))
                return
            try:
                await respond({'type': 5, 'data': {'flags': 64 if opts.get('privado') is True else 0}})
            except Exception:
                self.active.discard(key)
                raise
            self._start_job(self._video_job(i, prompt, opts.get('formato') or '16:9', opts.get('segundos') or 6), key)
            return
        if name == 'limpar':
            if key in self.active:
                await respond(immediate('Aguarde a resposta atual antes de limpar.', kind='error', me=me))
                return
            self.store.clear(f'{key}:private')
            self.store.clear(f'{key}:public')
            await respond(immediate('Pronto, limpei seus históricos público e privado deste canal. Podemos começar de novo! As mensagens que já estão no Discord continuam lá.', kind='info', me=me))
            return
        if name != 'perguntar':
            await respond(immediate('Comando desconhecido. Use /ajuda.', kind='error', me=me))
            return
        opts = {o['name']: o['value'] for o in (i.get('data') or {}).get('options') or []}
        prompt = opts.get('texto')
        pub = opts.get('privado') is not True
        error = self.reserve(i, prompt)
        if error:
            await respond(immediate(error, kind='error', me=me))
            return
        try:
            await respond({'type': 5, 'data': {'flags': 0 if pub else 64}})
        except Exception:
            self.active.discard(key)
            raise
        self.launch(i, prompt, pub, lambda text, _i=i, **kw: self.discord.edit(_i, text, **kw))

    async def mention(self, m, bot_id):
        author = m.get('author') or {}
        if author.get('bot') or m.get('webhook_id') or not self.authorized(m):
            return
        if m.get('guild_id') or m.get('message_reference'):
            m = await self._hydrate_reply(m, bot_id)
        prompt = addressed_prompt(m, bot_id) if m.get('guild_id') else (m.get('content') or '').strip()
        if prompt is None or not self.store.claim(f"m:{m.get('id')}"):
            return
        if not prompt:
            try:
                attachment = image_attachment(m)
            except AttachmentError as error:
                await self.discord.reply(m, str(error), kind='error')
                return
            if attachment:
                await self.discord.reply(m, 'Recebi a foto! Responda à mensagem que contém a foto dizendo o que quer mudar, ou envie foto e pedido juntos. No servidor, me marque também. Exemplo: “troque o fundo por uma praia”.', kind='info')
                return
            await self.discord.reply(m, 'Oi! Pode me contar o que você tem em mente. Se preferir uma resposta só pra você, use `/perguntar` com `privado:true`.', kind='info')
            return
        error = self.reserve(m, prompt)
        if error:
            await self.discord.reply(m, error, kind='error')
            return
        self.launch(m, prompt, True, lambda text, _m=m, **kw: self.discord.reply(_m, text, **kw))

    async def _hydrate_reply(self, m, bot_id):
        channel_id = m.get('channel_id')
        if is_addressed_to_bot(m, bot_id):
            if not (m.get('content') or '').strip() and m.get('id') and channel_id:
                try:
                    full = await self.discord.api(f'/channels/{channel_id}/messages/{m["id"]}')
                except Exception:
                    full = None
                if full:
                    m = {**m, 'content': full.get('content') or m.get('content'),
                         'mentions': full.get('mentions') or m.get('mentions'),
                         'attachments': full.get('attachments') or m.get('attachments')}
        ref = m.get('message_reference') or {}
        msg_id = ref.get('message_id')
        channel_id = ref.get('channel_id') or channel_id
        referenced = m.get('referenced_message') or {}
        referenced_author = referenced.get('author') or {}
        if (not msg_id or not channel_id or channel_id != m.get('channel_id')
                or (referenced_author.get('id') and 'attachments' in referenced)):
            return m
        try:
            original = await self.discord.api(f'/channels/{channel_id}/messages/{msg_id}')
        except Exception:
            return m
        if original:
            m = {**m, 'referenced_message': original}
        return m

    async def _typing(self, channel_id):
        try:
            await self.discord.api(f'/channels/{channel_id}/typing', 'POST')
        except Exception:
            pass

    async def _keep_typing(self, channel_id, stop):
        while channel_id and not stop.is_set():
            await self._typing(channel_id)
            try:
                await asyncio.wait_for(stop.wait(), timeout=8)
            except TimeoutError:
                continue

    def reserve(self, i, prompt):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            return 'Envie uma pergunta entre 1 e 4.000 caracteres.'
        if self.closing:
            return 'Estou reiniciando. Tenta de novo daqui a pouquinho.'
        if scope(i) in self.active:
            return 'Ainda estou preparando sua resposta anterior aqui. Pra interromper, use `/cancelar`.'
        if len(self.active) >= self.cfg.maxConcurrent:
            return 'Estou com alguns pedidos em andamento. Tenta de novo em alguns segundos.'
        reason = self.store.consume(identity(i), self.cfg)
        if reason:
            return reason
        self.active.add(scope(i))

    def launch(self, i, prompt, pub, reply):
        self._start_job(self._job(i, prompt, pub, reply), scope(i))

    def _start_job(self, coro, key=None):
        job = asyncio.get_running_loop().create_task(coro)
        self.jobs.add(job)
        if key:
            self.running[key] = job
        def finished(task):
            self.jobs.discard(task)
            if key and self.running.get(key) is task:
                self.running.pop(key, None)
                self.active.discard(key)
            if not task.cancelled() and task.exception():
                self.log('job_failed', error=type(task.exception()).__name__)
        job.add_done_callback(finished)
        return job

    async def _job(self, i, prompt, pub, reply):
        try:
            await self.generate(i, prompt, pub, reply)
        except Exception:
            self.log('job_failed')

    async def _export_job(self, i):
        try:
            rows = {visibility: self.store.history(f'{scope(i)}:{visibility}', self.cfg.ttl)
                    for visibility in ('public', 'private')}
            data = json.dumps({'conversas': rows}, ensure_ascii=False, indent=2).encode('utf-8')
            await self.discord.edit(i, 'Seu histórico deste canal está no arquivo. Apenas você pode ver esta resposta.',
                                    kind='info', document={'data': data, 'filename': 'historico.json', 'mime': 'application/json'})
        except Exception:
            self.log('history_export_failed')
            with suppress(Exception):
                await self.discord.edit(i, 'Não consegui exportar seu histórico.', kind='error')

    async def _action_job(self, i, name, opts):
        signal = AbortSignal.timeout(self.cfg.timeout)
        stage = 'prepare'
        try:
            async with asyncio.timeout(self.cfg.timeout / 1000):
                admin = name == 'canal' and self.cfg.enableServerTools and await event_is_admin(self.discord, i)
                ctx = {'guild_id': i.get('guild_id'), 'user_id': identity(i), 'admin': admin, 'signal': signal}
                actions = Actions(self.discord, self.banana, self.log, omni=self.omni)
                if name == 'cor':
                    tool, args = 'set_my_color', {'hex': opts.get('hex')}
                elif name == 'imagem':
                    ctx['image_attachment'] = image_attachment(i)
                    tool, args = 'generate_image', {'prompt': opts.get('texto')}
                    if opts.get('formato'):
                        args['aspect_ratio'] = opts['formato']
                elif opts.get('tipo') == 'category':
                    tool, args = 'create_category', {'name': opts.get('nome')}
                else:
                    tool, args = 'create_channel', {'name': opts.get('nome'), 'type': opts.get('tipo') or 'text'}
                    if opts.get('categoria'):
                        args['category'] = opts['categoria']
                stage = 'generate' if name == 'imagem' else 'execute'
                out = await actions.run(tool, args, ctx)
                stage = 'deliver'
                if not out.get('ok'):
                    await self.discord.edit(i, out.get('error') or 'Ação não concluída.', kind='error')
                elif ctx.get('image'):
                    await self.discord.edit(i, ctx.get('image_caption') or 'Aqui está a imagem.', kind='image', image=ctx['image'])
                else:
                    text = f"Sua cor agora é `{out['hex']}`." if out.get('hex') else (
                        f"Já existe: `{out.get('name')}`." if out.get('existed') else f"Criado: `{out.get('name')}`.")
                    await self.discord.edit(i, text, kind='info', color=ctx.get('color'))
        except asyncio.CancelledError:
            signal.abort()
            if not self.closing:
                with suppress(Exception):
                    await self.discord.edit(i, 'Pedido interrompido. Ações já concluídas permanecem.', kind='info')
            raise
        except Exception as error:
            self.log('action_failed', action=name, stage=stage, error=type(error).__name__,
                     status=getattr(error, 'status', 0), code=getattr(error, 'code', None))
            with suppress(Exception):
                if isinstance(error, AttachmentError):
                    message = str(error)
                elif name == 'imagem' and isinstance(error, (TimeoutError, AbortError)):
                    message = 'A edição da imagem demorou demais. Tente novamente com uma instrução mais simples.'
                elif name == 'imagem' and stage == 'deliver':
                    message = 'Não consegui enviar a resposta no Discord. Verifique a permissão de anexar arquivos e tente novamente.'
                elif name == 'imagem':
                    message = 'Falha ao processar a imagem. Tente novamente; se persistir, avise o administrador.'
                else:
                    message = 'Não consegui concluir a ação. Verifique /status e tente mais tarde.'
                await self.discord.edit(i, message, kind='error')
        finally:
            signal.close()
            self.active.discard(scope(i))

    async def _music_job(self, i, prompt):
        signal = AbortSignal.timeout(self.cfg.musicTimeout)
        generated = False
        try:
            async with asyncio.timeout(self.cfg.musicTimeout / 1000):
                audio = await self.lyria.generate(prompt, signal)
            generated = True
            await self.discord.edit(i, audio.caption, kind='audio', audio=audio)
            self.log('music_ok')
        except asyncio.CancelledError:
            signal.abort()
            if not self.closing:
                with suppress(Exception):
                    await self.discord.edit(i, 'Espera pela música interrompida. O provedor pode continuar a geração.', kind='info')
            raise
        except Exception as error:
            self.log('music_failed', service=getattr(error, 'service', None) or ('discord' if generated else 'lyria'),
                     status=getattr(error, 'status', 0), error=type(error).__name__)
            if generated:
                message = 'A música foi gerada, mas não consegui enviar o arquivo no Discord. Verifique a permissão de anexar arquivos.'
            elif isinstance(error, MusicError):
                message = str(error)
            elif getattr(error, 'status', None) == 429:
                message = 'O serviço de música está no limite de uso. Tente mais tarde.'
            elif getattr(error, 'status', None) in (401, 403, 404):
                message = 'O acesso ao Lyria precisa ser liberado ou revisado no projeto Google pelo administrador.'
            elif isinstance(error, (TimeoutError, AbortError)):
                message = 'A música demorou demais. Tente um pedido mais curto ou mais simples.'
            else:
                message = 'Não consegui gerar a música. Tente outro prompt ou tente novamente mais tarde.'
            with suppress(Exception):
                await self.discord.edit(i, message, kind='error')
        finally:
            signal.close()
            self.active.discard(scope(i))

    async def _video_job(self, i, prompt, aspect, seconds):
        signal = AbortSignal.timeout(self.cfg.videoTimeout)
        stop_typing = asyncio.Event()
        typer = asyncio.create_task(self._keep_typing(i.get('channel_id'), stop_typing))
        try:
            if not self.omni:
                await self.discord.edit(i, 'Geração de vídeo indisponível.', kind='error')
                return
            try:
                secs = int(seconds or 6)
            except (TypeError, ValueError):
                secs = 6
            async with asyncio.timeout(self.cfg.videoTimeout / 1000):
                video = await self.omni.generate(prompt, aspect or '16:9', secs, signal)
            await self.discord.edit(
                i,
                getattr(video, 'caption', None) or 'Aqui está o vídeo.',
                kind='video',
                video=video,
            )
            self.log('video_ok')
        except asyncio.CancelledError:
            signal.abort()
            if not self.closing:
                with suppress(Exception):
                    await self.discord.edit(i, 'Espera pelo vídeo interrompida. O provedor pode continuar a geração.', kind='info')
            raise
        except Exception as e:
            self.log(
                'generation_failed',
                service=getattr(e, 'service', None) or 'omni',
                status=getattr(e, 'status', None) or 0,
                error=type(e).__name__,
            )
            if getattr(e, 'status', None) == 429:
                msg = 'O serviço de vídeo está no limite de uso. Tente mais tarde.'
            elif getattr(e, 'status', None) in (401, 403):
                msg = 'O acesso ao Omni Flash precisa ser liberado no projeto Google (Vertex).'
            elif isinstance(e, (TimeoutError, AbortError)) or getattr(e, 'name', '') in ('TimeoutError', 'AbortError'):
                msg = 'O vídeo demorou demais. Tente um pedido mais simples.'
            else:
                msg = 'Não consegui gerar o vídeo. Tente novamente mais tarde.'
            try:
                await self.discord.edit(i, msg, kind='error')
            except Exception:
                self.log('discord_delivery_failed')
        finally:
            signal.close()
            stop_typing.set()
            typer.cancel()
            await asyncio.gather(typer, return_exceptions=True)
            self.active.discard(scope(i))

    async def generate(self, i, prompt, pub, reply):
        key = f"{scope(i)}:{'public' if pub else 'private'}"
        started = _now()
        signal = AbortSignal.timeout(self.cfg.timeout)
        ctx = {}
        sending_files = False
        try:
            async with asyncio.timeout(self.cfg.timeout / 1000):
                attachment = image_attachment(i)
                model_cfg = copy(self.cfg)
                model_cfg.system += (
                    '\n\nFOTO DESTE PEDIDO: há uma foto disponível. Se o pedido for editar ou transformar '
                    'a foto, chame generate_image; a ferramenta recebe a imagem original automaticamente. '
                    'Descreva só as alterações pedidas, preservando o restante. Você ainda não viu o conteúdo '
                    'da foto; não invente detalhes visuais. Se faltar a instrução de edição, pergunte o que mudar.'
                    if attachment else
                    '\n\nFOTO DESTE PEDIDO: nenhuma foto disponível. Para editar uma foto existente, '
                    'peça que o usuário anexe o arquivo junto com o pedido ou responda à mensagem da foto.'
                )
                base = context(self.store.history(key, self.cfg.ttl), prompt, model_cfg)
                admin = self.cfg.enableServerTools and await event_is_admin(self.discord, i)
                actions = Actions(self.discord, self.banana, self.log, omni=self.omni, search=self.search)
                ctx = {
                    'guild_id': i.get('guild_id'),
                    'user_id': identity(i),
                    'admin': admin,
                    'signal': signal,
                    'image_attachment': attachment,
                }
                self.log('job_start', admin=bool(admin))
                stop_typing = asyncio.Event()
                typer = asyncio.create_task(self._keep_typing(i.get('channel_id') if pub else None, stop_typing))
                try:
                    result_text, truncated, usage = await self._run_tools(list(base), prompt, actions, ctx, signal)
                finally:
                    stop_typing.set()
                    typer.cancel()
                    await asyncio.gather(typer, return_exceptions=True)
                result_text = _honest_server_text(ctx, result_text)
                saved = result_text
                if ctx.get('video'):
                    caption = result_text or ctx.get('video_caption') or 'Aqui está o vídeo.'
                    await reply(caption, kind='video', video=ctx['video'])
                    saved = caption
                    self.log('video_ok', duration_ms=_now() - started)
                elif ctx.get('image'):
                    caption = result_text or ctx.get('image_caption') or 'Aqui está a imagem.'
                    await reply(caption, kind='image', image=ctx['image'])
                    saved = caption
                    self.log('image_ok', duration_ms=_now() - started)
                elif ctx.get('color_hex'):
                    text = result_text or f"Sua cor neste servidor agora é `{ctx['color_hex']}`. Só o seu perfil mudou."
                    await reply(text, kind='info', color=ctx.get('color'))
                    saved = text
                    self.log('generation_ok', duration_ms=_now() - started)
                else:
                    prepared = prepare_response(result_text, truncated=truncated)
                    shown = prepared.text
                    sending_files = prepared.document is not None
                    delivery = {'document': prepared.document} if sending_files else {}
                    sources = _source_section(shown, ctx)
                    if sources and not shown:
                        shown = 'Encontrei isto. As fontes estão na outra página.'
                    if sources:
                        page_id = self.store.save_pages(text_preview(shown), sources)
                        await reply(shown, kind='response', components=page_row(page_id, 0), **delivery)
                    else:
                        await reply(shown, kind='response', **delivery)
                    # Retain source code, not only the visible attachment labels, for follow-up edits.
                    saved = _with_sources(prepared.history or shown, ctx)
                    fields = {'duration_ms': _now() - started}
                    if isinstance(usage, dict) and usage.get('total_tokens') is not None:
                        fields['total_tokens'] = usage.get('total_tokens')
                    elif usage is not None and getattr(usage, 'total_tokens', None) is not None:
                        fields['total_tokens'] = usage.total_tokens
                    self.log('generation_ok', **fields)
                self.store.save(key, [*base[1:], {'role': 'assistant', 'content': saved}][-self.cfg.historyPairs * 2:])
        except asyncio.CancelledError:
            signal.abort()
            if not self.closing:
                with suppress(Exception):
                    await reply(_honest_server_text(ctx, 'Pedido interrompido.'), kind='info')
            raise
        except Exception as e:
            self.log('generation_failed', service=getattr(e, 'service', None) or 'internal', status=getattr(e, 'status', None) or 0)
            name = getattr(e, 'name', None) or type(e).__name__
            if isinstance(e, AttachmentError):
                message = str(e)
            elif sending_files and getattr(e, 'service', None) == 'discord' and getattr(e, 'status', None) == 403:
                message = 'Não consegui enviar os arquivos no Discord. Verifique a permissão de anexar arquivos neste canal e tente novamente.'
            elif getattr(e, 'status', None) == 429:
                if getattr(e, 'service', None) == 'discord':
                    message = 'O Discord limitou as alterações (muitos canais de uma vez). Espere uns 30 segundos e peça o que faltou.'
                else:
                    message = 'O serviço de IA está no limite de uso. Tente novamente mais tarde.'
            elif 'bloqueou' in str(e).lower():
                message = 'O modelo recusou esse conteúdo.'
            elif getattr(e, 'status', None) in (401, 403):
                message = 'O acesso ao serviço precisa ser revisado pelo administrador.'
            elif sending_files and getattr(e, 'service', None) == 'discord':
                message = 'O código foi gerado, mas não consegui enviar os arquivos no Discord. Tente novamente em alguns instantes.'
            elif name in ('TimeoutError', 'AbortError') or isinstance(e, (TimeoutError, AbortError)):
                message = 'A resposta demorou mais do que eu podia esperar. Tenta de novo ou divide o pedido em partes menores.'
            else:
                message = 'Não consegui terminar a resposta desta vez. Tenta de novo daqui a pouco.'
            if ctx.get('tool_results'):
                message += '\n\n' + _honest_server_text(ctx, '')
            try:
                await reply(message, kind='error')
            except TypeError:
                await reply(message)
            except Exception:
                self.log('discord_delivery_failed')
        finally:
            signal.close()
            self.active.discard(scope(i))

    async def _run_tools(self, messages, prompt, actions, ctx, signal):
        selected = [tool for tool in TOOLS if (
            tool['function']['name'] not in SERVER_TOOLS or ctx.get('admin')
        ) and (tool['function']['name'] != 'generate_image' or self.banana)
          and (tool['function']['name'] != 'generate_video' or self.omni)
          and (tool['function']['name'] != 'web_search' or self.search)
          and (tool['function']['name'] != 'set_my_color' or ctx.get('guild_id'))]
        allowed = {t['function']['name'] for t in selected}
        usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        count, cache = 0, {}
        for _round in range(self.cfg.maxToolRounds):
            signal.check()
            if len(json.dumps(messages, ensure_ascii=False)) > self.cfg.contextChars:
                return 'O contexto atingiu o limite. Peça uma etapa menor ou use /limpar.', False, usage
            result = await self.vertex.complete(messages, signal, tools=selected)
            get = result.get if isinstance(result, dict) else lambda k, d=None: getattr(result, k, d)
            text, truncated = get('text', '') or '', bool(get('truncated', False))
            current_usage = get('usage')
            if isinstance(current_usage, dict):
                self.store.record_tokens(ctx['user_id'], current_usage)
                for field in usage:
                    value = current_usage.get(field)
                    if type(value) is int and value >= 0:
                        usage[field] += value
            calls = get('tool_calls') or []
            self.log('model_complete', tools=len(calls), round=_round + 1)
            if not calls:
                return text, truncated, usage
            if count + len(calls) > self.cfg.maxToolCalls:
                return 'O pedido atingiu o limite de ferramentas. Peça as etapas restantes separadamente.', False, usage
            normalized = []
            for index, call in enumerate(calls):
                name, args, call_id = _parse_call(call, index)
                normalized_call = {'id': call_id, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
                # Gemini requires its opaque thought signatures on the next tool
                # round. Keep them in API context, never in replies or logs.
                if isinstance(call, dict) and isinstance(call.get('extra_content'), dict):
                    normalized_call['extra_content'] = call['extra_content']
                normalized.append(normalized_call)
            assistant = {'role': 'assistant', 'content': text or None, 'tool_calls': normalized}
            if isinstance(get('extra_content'), dict):
                assistant['extra_content'] = get('extra_content')
            messages.append(assistant)
            names, outcomes = set(), []
            for index, call in enumerate(normalized):
                signal.check()
                name, args, call_id = _parse_call(call, index)
                names.add(name)
                count += 1
                cache_key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                if name not in allowed:
                    out = {'ok': False, 'error': 'Ferramenta não disponível para este pedido.'}
                elif name != 'list_server' and cache_key in cache:
                    out = cache[cache_key]
                else:
                    out = await actions.run(name, args, ctx)
                    if out.get('ok'):
                        cache[cache_key] = out
                outcomes.append(out)
                ctx.setdefault('tool_results', []).append({'tool': name, **out})
                self.log('tool', name=name if name in allowed else 'unavailable', ok=bool(out.get('ok')))
                messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(out, ensure_ascii=False)})
                if out.get('rate_limited') or any(x.get('rate_limited') for x in out.get('results', []) if isinstance(x, dict)):
                    return 'O Discord limitou as alterações. Tente as etapas restantes mais tarde.', False, usage
            if names <= SIDE_EFFECT_TOOLS:
                notes = [out.get('caption') or (f"Cor aplicada: {out['hex']}." if out.get('hex') else '')
                         if out.get('ok') else out.get('error', 'A ação não foi concluída.') for out in outcomes]
                return '\n'.join(filter(None, notes)), truncated, usage
        return 'O pedido atingiu o limite de etapas. Peça para continuar o que faltou.', False, usage

    async def turn_page(self, i, respond):
        me = getattr(self.discord, 'me', None)
        if not self.authorized(i):
            await respond(immediate('Este usuário, servidor ou canal não está autorizado.', kind='error', me=me))
            return
        parsed = _parse_page((i.get('data') or {}).get('custom_id'))
        if not parsed:
            await respond(immediate('Este botão não está mais disponível.', kind='error', me=me))
            return
        if not self.store.claim(f"i:{i.get('id')}"):
            await respond(immediate('Esta interação já foi recebida.', kind='error', me=me))
            return
        page_id, page = parsed
        stored = self.store.pages(page_id)
        if not stored:
            await respond(immediate('Essas páginas expiraram. Peça de novo para ver as fontes.', kind='info', me=me))
            return
        text = stored['answer'] if page == 0 else stored['sources']
        await respond({
            'type': 7,
            'data': {
                'embeds': [build_embed(text, kind='response', title='Fontes' if page == 1 else None)],
                'components': page_row(page_id, page),
                'allowed_mentions': {'parse': []},
            },
        })

    async def drain(self):
        self.closing = True
        if self.jobs:
            await asyncio.gather(*list(self.jobs), return_exceptions=True)


def _parse_page(custom_id):
    match = re.fullmatch(r'pg:([0-9a-f]{16}):([01])', custom_id or '')
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _source_section(text, ctx):
    text = text or ''
    sources, queries, seen = [], [], set()
    for row in ctx.get('tool_results') or []:
        if row.get('tool') != 'web_search' or not row.get('ok'):
            continue
        for query in row.get('searches') or []:
            if isinstance(query, str) and query.strip() and query.strip() not in queries:
                queries.append(query.strip())
        for src in row.get('sources') or []:
            if not isinstance(src, dict):
                continue
            url = src.get('url')
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')) or url in seen or url in text:
                continue
            seen.add(url)
            title = src.get('title') if isinstance(src.get('title'), str) else ''
            title = re.sub(r'\s+', ' ', title).strip()[:80] or 'fonte'
            sources.append((title, url))
            if len(sources) >= 5:
                break
        if len(sources) >= 5:
            break
    lines = []
    if sources:
        lines.extend(f'• {title} — {url}' for title, url in sources)
    fresh = [query for query in queries[:3] if f'q={quote_plus(query)}' not in text]
    if fresh:
        links = ', '.join(f'{query} (https://www.google.com/search?q={quote_plus(query)})' for query in fresh)
        lines.append(f'Pesquisa Google: {links}')
    return '\n'.join(lines)


def _with_sources(text, ctx):
    section = _source_section(text, ctx)
    if not section:
        return text or ''
    return (text or '').rstrip() + '\n\n**Fontes**\n' + section


def _honest_server_text(ctx, text):
    action_tools = {'create_category', 'create_channel', 'create_channels', 'edit_channel', 'apply_channel_names'}
    rows = []
    for r in ctx.get('tool_results') or []:
        if r.get('results') and isinstance(r.get('results'), list):
            for inner in r['results']:
                rows.append({'tool': r.get('tool'), **(inner or {})})
        elif r.get('tool') in action_tools or str(r.get('tool') or '').startswith('fallback_'):
            rows.append(r)
    if not rows:
        return text
    ok = [r for r in rows if r.get('ok')]
    bad = [r for r in rows if not r.get('ok')]
    lines = []
    if ok:
        lines.append('**Feito**')
        for row in ok:
            label = row.get('name') or row.get('id') or row.get('tool')
            changed = row.get('changed')
            extra = f" ({', '.join(changed)})" if changed else ''
            lines.append(f"• `{label}`{extra}")
    if bad:
        lines.append('**Não aplicado**')
        for row in bad:
            lines.append(f"• {row.get('error') or row.get('tool')}")
    return '\n'.join(lines)


def _parse_call(call, index):
    fn = call.get('function') if isinstance(call, dict) else None
    name = (fn or {}).get('name') if fn else ''
    raw = (fn or {}).get('arguments') if fn else '{}'
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        args = None
    call_id = (call.get('id') if isinstance(call, dict) else None) or f'call{index}'
    return name, args, call_id


def _now():
    from time import time
    return int(time() * 1000)
