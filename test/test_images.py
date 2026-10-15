import asyncio
import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from test import test_core
from test.test_core import cfg, event
from attachments import AttachmentError, MAX_IMAGE_BYTES, download_image, image_attachment
from banana import Banana
from bot import scope
from discord import Discord, USER_AGENT, commands
from net import AbortError, AbortSignal, FormData, Response, _sync_fetch


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=')
EDITED = PNG + b'edited'


def photo(**changes):
    return {'id': '333', 'filename': 'foto.png', 'content_type': 'image/png', 'size': len(PNG),
            'url': 'https://cdn.discordapp.com/attachments/111/333/foto.png?ex=123&hm=signed', **changes}


def with_photo(interaction, attachment=None):
    attachment = photo() if attachment is None else attachment
    interaction['data']['options'].append({'name': 'foto', 'type': 11, 'value': attachment['id']})
    interaction['data']['resolved'] = {'attachments': {attachment['id']: attachment}}
    return interaction


TARGET_USER = '175928847299117063'
AVATAR_HASH = '1234567890abcdef1234567890abcdef'
GUILD_ID = '123456789012345678'


def with_avatar(interaction, *, user=None, member=None):
    user = {'id': TARGET_USER, 'avatar': AVATAR_HASH, 'discriminator': '0'} if user is None else user
    interaction['data']['options'].append({'name': 'usuario', 'type': 6, 'value': TARGET_USER})
    resolved = interaction['data'].setdefault('resolved', {})
    resolved['users'] = {TARGET_USER: user}
    if member is not None:
        resolved['members'] = {TARGET_USER: member}
    return interaction


def animated_avatar(format):
    frames = [Image.new('RGBA', (4, 4), (0, 0, 0, 0)) for _ in range(2)]
    try:
        frames[0].putpixel((1, 1), (255, 0, 0, 255))
        frames[1].putpixel((1, 1), (0, 0, 255, 255))
        with io.BytesIO() as output:
            frames[0].save(output, format=format, save_all=True, append_images=frames[1:],
                           duration=80, loop=0, lossless=True)
            return output.getvalue()
    finally:
        for frame in frames:
            frame.close()


class ImageTest(unittest.IsolatedAsyncioTestCase):
    fixture = test_core.CoreTest.fixture
    _respond = test_core.CoreTest._respond

    def banana(self, calls, body=PNG, status=200):
        async def fetch(url, opts):
            calls.append((url, opts))
            if url.startswith('https://cdn.discordapp.com/'):
                return Response(body, status=status)
            return Response.json({'candidates': [{'content': {'parts': [
                {'text': 'Foto editada'},
                {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(EDITED).decode('ascii')}},
            ]}}]})
        return Banana(cfg(), SimpleNamespace(token=AsyncMock(return_value='google-token')), fetch)

    def image_fixture(self, calls, **kwargs):
        async def complete(messages):
            self.assertIn('há uma foto disponível', messages[0]['content'])
            self.assertNotIn(photo()['url'], json.dumps(messages))
            return {'tool_calls': [{'id': 'edit', 'function': {'name': 'generate_image', 'arguments':
                    json.dumps({'prompt': 'Change the background to a beach; preserve the subject.'})}}]}
        return self.fixture(complete, banana=self.banana(calls, **kwargs))

    def assert_edited(self, f, calls, attachment=None):
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], (attachment or photo())['url'])
        self.assertNotIn('authorization', calls[0][1].get('headers', {}))
        self.assertEqual(calls[0][1]['redirect'], 'error')
        self.assertEqual(calls[0][1]['max_bytes'], MAX_IMAGE_BYTES)
        payload = json.loads(calls[1][1]['body'])
        source = payload['contents'][0]['parts'][1]['inlineData']
        self.assertEqual(base64.b64decode(source['data']), PNG)
        self.assertEqual(source['mimeType'], 'image/png')
        self.assertEqual(f.extras[0]['image'].data, EDITED)
        self.assertEqual(f.extras[0]['kind'], 'image')
        self.assertFalse(f.b.active)
        return payload

    async def test_slash_edits_original_and_delivers_edited_attachment_privately(self):
        calls, uploads = [], []
        f = self.image_fixture(calls)
        attachment = photo(url=photo()['url'].replace('/attachments/', '/ephemeral-attachments/'))
        i = with_photo(event(name='imagem', text='Troque o fundo por uma praia'), attachment)
        i['data']['options'].append({'name': 'privado', 'value': True})

        async def discord_fetch(_url, opts):
            uploads.append(opts['body'])
            return Response.json({'id': 'output'})

        delivery = Discord('discord-token', discord_fetch)
        original_edit = f.b.discord.edit

        async def edit(interaction, text, **kwargs):
            await original_edit(interaction, text, **kwargs)
            await delivery.edit(interaction, text, **kwargs)

        f.b.discord.edit = edit
        await f.b.interaction(i, await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        payload = self.assert_edited(f, calls, attachment)
        self.assertNotIn('imageConfig', payload['generationConfig'])
        self.assertEqual(f.responses, [{'type': 5, 'data': {'flags': 64}}])
        self.assertFalse(f.calls)  # /imagem does not need the text model.
        self.assertIsInstance(uploads[0], FormData)
        self.assertEqual(uploads[0].get('files[0]').data, EDITED)
        sent = json.loads(uploads[0].get('payload_json'))
        self.assertEqual(sent['embeds'][0]['image']['url'], 'attachment://imagem.png')
        self.assertEqual(sent['allowed_mentions']['parse'], [])

    async def test_perguntar_uses_photo_and_never_persists_signed_url_or_bytes(self):
        calls = []
        f = self.image_fixture(calls)
        i = with_photo(event(text='Edite esta foto'))
        i['data']['options'].append({'name': 'privado', 'value': True})
        await f.b.interaction(i, await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assert_edited(f, calls)
        history = f.s.history(f'{scope(i)}:private', f.c.ttl)
        self.assertEqual(history[0]['content'], 'Edite esta foto')
        self.assertNotIn('signed', json.dumps(history))
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(history))
        self.assertFalse(f.s.history(f'{scope(i)}:public', f.c.ttl))

    async def test_selected_avatar_is_edited_in_guild_and_dm_with_user_caption(self):
        for guild in (GUILD_ID, None):
            with self.subTest(guild=guild):
                calls = []
                f = self.image_fixture(calls)
                i = with_avatar(event(name='imagem', text='Coloque óculos escuros'))
                i['guild_id'] = guild
                if guild is None:
                    i.pop('member')
                    i['user'] = {'id': 'u'}
                i['data']['options'].append({'name': 'privado', 'value': True})
                expected_url = f'https://cdn.discordapp.com/avatars/{TARGET_USER}/{AVATAR_HASH}.png?size=1024'
                await f.b.interaction(i, await self._respond(f))
                self.assertEqual(f.responses, [{'type': 5, 'data': {'flags': 64}}])
                await asyncio.gather(*list(f.b.jobs))
                payload = self.assert_edited(f, calls, {'url': expected_url})
                self.assertEqual(payload['contents'][0]['parts'][0]['text'], 'Coloque óculos escuros')
                self.assertNotIn('imageConfig', payload['generationConfig'])
                self.assertIn(f'versão editada do avatar de <@{TARGET_USER}>', f.outputs[0])
                self.assertFalse(f.calls)
                self.assertEqual(f.s.count('u'), 1)
                self.assertEqual(f.s.count(TARGET_USER), 0)

    async def test_guild_avatar_precedes_global_and_animated_avatar_is_static_png(self):
        calls = []
        f = self.image_fixture(calls)
        guild_hash = 'a_' + 'f' * 32
        i = with_avatar(event(name='imagem'), member={'avatar': guild_hash})
        i['guild_id'] = GUILD_ID
        expected_url = f'https://cdn.discordapp.com/guilds/{GUILD_ID}/users/{TARGET_USER}/avatars/{guild_hash}.png?size=1024'
        await f.b.interaction(i, await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assert_edited(f, calls, {'url': expected_url})

    async def test_animated_avatar_first_frame_reaches_banana_as_real_png(self):
        for format in ('GIF', 'WEBP'):
            for guild_avatar in (False, True):
                with self.subTest(format=format, guild_avatar=guild_avatar):
                    calls = []
                    animation = animated_avatar(format)
                    with Image.open(io.BytesIO(animation)) as original:
                        self.assertEqual(original.n_frames, 2)
                    f = self.image_fixture(calls, body=animation)
                    avatar_hash = 'a_' + AVATAR_HASH
                    i = with_avatar(event(name='imagem', text='Coloque óculos escuros'),
                                    user={'id': TARGET_USER, 'avatar': avatar_hash},
                                    member={'avatar': avatar_hash} if guild_avatar else None)
                    i['guild_id'] = GUILD_ID if guild_avatar else None
                    await f.b.interaction(i, await self._respond(f))
                    await asyncio.gather(*list(f.b.jobs))
                    self.assertEqual(len(calls), 2)
                    self.assertIn(f'{avatar_hash}.png?size=1024', calls[0][0])
                    self.assertNotIn('animated=true', calls[0][0])
                    payload = json.loads(calls[1][1]['body'])
                    self.assertEqual(payload['contents'][0]['parts'][0]['text'], 'Coloque óculos escuros')
                    source = payload['contents'][0]['parts'][1]['inlineData']
                    self.assertEqual(source['mimeType'], 'image/png')
                    with Image.open(io.BytesIO(base64.b64decode(source['data']))) as converted:
                        self.assertEqual(converted.format, 'PNG')
                        self.assertEqual(converted.n_frames, 1)
                        self.assertEqual(converted.getpixel((1, 1)), (255, 0, 0, 255))
                        self.assertEqual(converted.getpixel((0, 0))[3], 0)
                    self.assertEqual(f.extras[0]['image'].data, EDITED)
                    self.assertFalse(f.b.active)

    async def test_avatar_conversion_detects_actual_file_format_even_for_png_url(self):
        avatar = image_attachment(with_avatar(event(name='imagem')))
        self.assertIn('.png?', avatar['url'])
        for format in ('GIF', 'WEBP'):
            with self.subTest(format=format):
                result = await download_image(avatar, fetcher=AsyncMock(return_value=Response(animated_avatar(format))))
                self.assertEqual(result['mime'], 'image/png')
                with Image.open(io.BytesIO(result['data'])) as converted:
                    self.assertEqual(converted.n_frames, 1)
                    self.assertEqual(converted.getpixel((1, 1)), (255, 0, 0, 255))

    async def test_static_webp_fallback_preserves_the_selected_animated_avatar(self):
        for failure in ('http', 'invalid', 'network'):
            for guild in (None, GUILD_ID):
                with self.subTest(failure=failure, guild=guild):
                    calls = []
                    f = self.image_fixture(calls, body=animated_avatar('WEBP'))
                    original = f.b.banana.fetcher
                    async def fetch(url, opts):
                        if url.startswith('https://cdn.discordapp.com/') and '.png?' in url:
                            calls.append((url, opts))
                            if failure == 'network':
                                raise OSError('private network details')
                            return Response(b'not a PNG', status=415 if failure == 'http' else 200)
                        return await original(url, opts)
                    f.b.banana.fetcher = fetch
                    avatar_hash = 'a_' + AVATAR_HASH
                    i = with_avatar(event(name='imagem'), user={'id': TARGET_USER, 'avatar': avatar_hash},
                                    member={'avatar': avatar_hash} if guild else None)
                    i['guild_id'] = guild
                    await f.b.interaction(i, await self._respond(f))
                    await asyncio.gather(*list(f.b.jobs))
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(calls[1][0], calls[0][0].replace('.png?', '.webp?'))
                    for url, opts in calls[:2]:
                        self.assertIn(f'/{TARGET_USER}/', url)
                        self.assertIn(avatar_hash, url)
                        self.assertNotIn('animated=true', url)
                        self.assertNotIn('authorization', opts['headers'])
                    source = json.loads(calls[2][1]['body'])['contents'][0]['parts'][1]['inlineData']
                    self.assertEqual(source['mimeType'], 'image/png')
                    with Image.open(io.BytesIO(base64.b64decode(source['data']))) as image:
                        self.assertEqual(image.n_frames, 1)
                        self.assertEqual(image.getpixel((1, 1)), (255, 0, 0, 255))
                    self.assertEqual(f.extras[0]['image'].data, EDITED)
                    self.assertFalse(f.b.active)

    async def test_decoder_runtime_error_is_reported_without_generic_action_failure(self):
        calls, logs = [], []
        f = self.image_fixture(calls, body=animated_avatar('WEBP'))
        f.b.log = lambda event, **fields: logs.append((event, fields))
        i = with_avatar(event(name='imagem'), user={'id': TARGET_USER, 'avatar': 'a_' + AVATAR_HASH})
        with patch('attachments.Image.open', side_effect=RuntimeError('private decoder failure')):
            await f.b.interaction(i, await self._respond(f))
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(calls), 2)
        self.assertIn('converter o avatar', f.outputs[0])
        self.assertIn(('image_failed', {'code': 'avatar_decode', 'error': 'AttachmentError', 'status': 0}), logs)
        self.assertNotIn('private decoder', json.dumps(logs) + f.outputs[0])
        f.b.banana.auth.token.assert_not_called()

    async def test_avatar_cancellation_does_not_start_fallback_or_generation(self):
        avatar = image_attachment(with_avatar(event(name='imagem')))
        signal = AbortSignal()
        async def fetch(url, opts):
            signal.abort()
            raise AbortError()
        fetcher = AsyncMock(side_effect=fetch)
        with self.assertRaises(AbortError):
            await download_image(avatar, signal=signal, fetcher=fetcher)
        self.assertEqual(fetcher.await_count, 1)

    async def test_missing_or_blocked_generated_image_gets_specific_message_and_safe_log(self):
        for response, code, message in (
                ({'candidates': [{'content': {'parts': [{'text': 'private response text'}]}, 'finishReason': 'STOP'}]}, 'image_missing', 'não devolveu uma imagem'),
                ({'candidates': [{'finishReason': 'IMAGE_SAFETY'}]}, 'image_blocked', 'recusou essa edição'),
                ({'promptFeedback': {'blockReason': 'SAFETY'}}, 'image_blocked', 'recusou essa edição'),
                ({'candidates': [{'content': {'parts': [{'inlineData': {'data': 'invalid!'}}]}}]}, 'image_response', 'imagem inválida')):
            with self.subTest(code=code, response=response):
                calls, logs = [], []
                f = self.image_fixture(calls, body=animated_avatar('GIF'))
                original = f.b.banana.fetcher
                async def fetch(url, opts):
                    if url.startswith('https://cdn.discordapp.com/'):
                        return await original(url, opts)
                    calls.append((url, opts))
                    return Response.json(response)
                f.b.banana.fetcher = fetch
                f.b.log = lambda event, **fields: logs.append((event, fields))
                await f.b.interaction(with_avatar(event(name='imagem')), await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assertEqual(len(calls), 2)
                self.assertIn(message, f.outputs[0])
                self.assertNotIn('Não consegui concluir a ação', f.outputs[0])
                self.assertNotIn('image', f.extras[0])
                self.assertTrue(any(fields.get('code') == code for _, fields in logs))
                self.assertNotIn('private response text', str(logs) + f.outputs[0])
                self.assertFalse(f.b.active)

    async def test_image_access_error_is_not_misreported_as_discord_permissions(self):
        calls = []
        f = self.image_fixture(calls)
        original = f.b.banana.fetcher
        async def fetch(url, opts):
            if url.startswith('https://cdn.discordapp.com/'):
                return await original(url, opts)
            return Response('private Google error', status=403)
        f.b.banana.fetcher = fetch
        await f.b.interaction(with_avatar(event(name='imagem')), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertIn('acesso ao Nano Banana', f.outputs[0])
        self.assertNotIn('Gerenciar Cargos', f.outputs[0])
        self.assertFalse(f.b.active)

    async def test_image_deadline_reports_timeout_instead_of_generic_action_failure(self):
        f = self.image_fixture([])
        f.c.timeout = 10
        async def generate(*args, **kwargs):
            await asyncio.Future()
        f.b.banana.generate = generate
        logs = []
        f.b.log = lambda event, **fields: logs.append((event, fields))
        await f.b.interaction(with_avatar(event(name='imagem')), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertIn('imagem demorou demais', f.outputs[0])
        self.assertTrue(any(fields.get('stage') == 'generate' for _, fields in logs))
        self.assertFalse(f.b.active)

    async def test_corrupt_animated_avatar_is_not_sent_to_banana(self):
        for body in (b'GIF89a', b'RIFF1234WEBPbad', b'<html>error</html>'):
            with self.subTest(body=body):
                calls = []
                f = self.image_fixture(calls, body=body)
                i = with_avatar(event(name='imagem'), user={'id': TARGET_USER, 'avatar': 'a_' + AVATAR_HASH})
                await f.b.interaction(i, await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assertEqual(len(calls), 2)
                self.assertIn('converter o avatar para PNG', f.outputs[0])
                f.b.banana.auth.token.assert_not_called()
                self.assertNotIn('image', f.extras[0])
                self.assertFalse(f.b.active)

    async def test_avatar_conversion_limits_pixels_and_png_output_size(self):
        avatar = image_attachment(with_avatar(event(name='imagem')))
        animation = animated_avatar('GIF')
        fetch = AsyncMock(return_value=Response(animation))
        with patch('attachments.MAX_AVATAR_PIXELS', 15):
            with self.assertRaisesRegex(AttachmentError, 'resolução'):
                await download_image(avatar, fetcher=fetch)

        # A compressed animation may expand beyond the input size when saved as PNG.
        with Image.new('RGBA', (16, 16)) as frame, io.BytesIO() as output:
            frame.putdata([(x * 17, y * 17, (x + y) * 8, 255) for y in range(16) for x in range(16)])
            frame.save(output, format='WEBP', quality=1)
            raw = output.getvalue()
        with patch('attachments.MAX_IMAGE_BYTES', len(raw)):
            with self.assertRaisesRegex(AttachmentError, 'convertido para PNG'):
                await download_image(avatar, fetcher=AsyncMock(return_value=Response(raw)))

    async def test_default_avatars_are_editable_for_both_username_systems(self):
        for discriminator, index in (('0', 2), ('1338', 3)):
            with self.subTest(discriminator=discriminator):
                calls = []
                f = self.image_fixture(calls)
                i = with_avatar(event(name='imagem'), user={
                    'id': TARGET_USER, 'avatar': None, 'discriminator': discriminator})
                expected_url = f'https://cdn.discordapp.com/embed/avatars/{index}.png?size=1024'
                await f.b.interaction(i, await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assert_edited(f, calls, {'url': expected_url})

    async def test_avatar_and_file_cannot_silently_replace_each_other(self):
        calls = []
        f = self.image_fixture(calls)
        i = with_avatar(with_photo(event(name='imagem')))
        await f.b.interaction(i, await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertIn('Escolha usuario', f.outputs[0])
        self.assertEqual(f.extras[0]['kind'], 'error')
        self.assertFalse(calls or f.b.active)

    async def test_missing_or_invalid_selected_user_never_edits_someone_else(self):
        for user in ({}, {'id': '999999999999999999', 'avatar': AVATAR_HASH},
                     {'id': TARGET_USER}, {'id': TARGET_USER, 'avatar': '../../secret'},
                     {'id': TARGET_USER, 'avatar': None, 'discriminator': 'bad'}):
            with self.subTest(user=user):
                calls = []
                f = self.image_fixture(calls)
                i = with_avatar(event(name='imagem'), user=user)
                await f.b.interaction(i, await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assertEqual(f.extras[0]['kind'], 'error')
                self.assertFalse(calls or f.calls or f.b.active)

    async def test_avatar_download_failure_does_not_generate_an_unrelated_image(self):
        calls = []
        f = self.image_fixture(calls, status=404)
        await f.b.interaction(with_avatar(event(name='imagem')), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(calls), 2)
        self.assertIn('baixar o avatar', f.outputs[0])
        self.assertNotIn('Anexe', f.outputs[0])
        self.assertNotIn('image', f.extras[0])
        f.b.banana.auth.token.assert_not_called()

    async def test_unknown_avatar_size_is_bounded_and_avatar_urls_stay_restricted(self):
        avatar = image_attachment(with_avatar(event(name='imagem')))
        fetch = AsyncMock(return_value=Response(PNG + b'excess'))
        with patch('attachments.MAX_IMAGE_BYTES', len(PNG)):
            with self.assertRaisesRegex(AttachmentError, '7 MB'):
                await download_image(avatar, fetcher=fetch)
        for url in ('https://example.com/avatars/1/avatar.png',
                    'https://cdn.discordapp.com/banners/175928847299117063/avatar.png',
                    photo()['url'], 'https://cdn.discordapp.com/embed/avatars/6.png'):
            with self.subTest(url=url):
                fetch = AsyncMock()
                with self.assertRaises(AttachmentError):
                    await download_image({**avatar, 'url': url}, fetcher=fetch)
                fetch.assert_not_called()
        # A normal attachment with a missing size must still fail validation.
        with self.assertRaises(AttachmentError):
            await download_image(photo(size=None), fetcher=AsyncMock())

    def test_avatar_selector_is_optional_and_available_everywhere(self):
        command = next(c for c in commands if c['name'] == 'imagem')
        option = next(o for o in command['options'] if o['name'] == 'usuario')
        self.assertEqual(option['type'], 6)
        self.assertFalse(option.get('required'))
        self.assertEqual(set(command['contexts']), {0, 1, 2})
        self.assertEqual(set(command['integration_types']), {0, 1})

    async def test_photo_in_mention_and_dm_reaches_banana(self):
        for guild in (None, 'g'):
            with self.subTest(guild=guild):
                calls = []
                f = self.image_fixture(calls)
                m = {'id': '1', 'channel_id': 'c', 'author': {'id': 'u'},
                     'content': 'Troque o fundo', 'attachments': [photo()]}
                if guild:
                    m.update(guild_id=guild, content='<@bot> Troque o fundo')
                await f.b.mention(m, 'bot')
                await asyncio.gather(*list(f.b.jobs))
                self.assert_edited(f, calls)

    async def test_disabled_images_never_download_attachment(self):
        calls = []
        f = self.image_fixture(calls)
        f.b.banana = None

        async def complete(_messages, _signal, *, tools):
            self.assertNotIn('generate_image', {t['function']['name'] for t in tools})
            return {'text': 'Edição indisponível.'}

        f.b.vertex.complete = complete
        await f.b.interaction(with_photo(event()), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertFalse(calls)
        self.assertNotIn('image', f.extras[0])

    async def test_next_turn_does_not_reuse_a_previous_photo(self):
        calls = []
        f = self.image_fixture(calls)
        await f.b.interaction(with_photo(event()), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assert_edited(f, calls)

        async def complete(messages, *_args, **_kwargs):
            self.assertIn('nenhuma foto disponível', messages[0]['content'])
            self.assertNotIn('há uma foto disponível', messages[0]['content'])
            return {'text': 'Anexe a foto ou responda à mensagem com a foto.'}

        f.b.vertex.complete = complete
        await f.b.interaction(event(id='2', text='Edite novamente'), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('image', f.extras[-1])

    async def test_reply_to_photo_is_hydrated_even_with_explicit_bot_mention(self):
        calls = []
        f = self.image_fixture(calls)
        paths = []

        async def api(path, *_args):
            paths.append(path)
            if path == '/channels/c/messages/original':
                return {'id': 'original', 'author': {'id': 'u'}, 'attachments': [photo()]}

        f.b.discord.api = api
        m = {'id': '2', 'channel_id': 'c', 'guild_id': 'g', 'author': {'id': 'u'},
             'content': '<@bot> Deixe o fundo azul',
             'message_reference': {'message_id': 'original', 'channel_id': 'c'}}
        await f.b.mention(m, 'bot')
        await asyncio.gather(*list(f.b.jobs))
        self.assert_edited(f, calls)
        self.assertIn('/channels/c/messages/original', paths)

    async def test_reply_to_bot_image_can_be_edited_again(self):
        calls = []
        f = self.image_fixture(calls)
        m = {'id': '2', 'channel_id': 'c', 'guild_id': 'g', 'author': {'id': 'u'},
             'content': 'Agora deixe o fundo azul',
             'message_reference': {'message_id': 'original', 'channel_id': 'c'},
             'referenced_message': {'author': {'id': 'bot', 'bot': True}, 'attachments': [photo()]}}
        await f.b.mention(m, 'bot')
        await asyncio.gather(*list(f.b.jobs))
        self.assert_edited(f, calls)

    async def test_photo_without_text_asks_for_edit_without_inference_or_quota(self):
        for guild in (None, 'g'):
            with self.subTest(guild=guild):
                calls = []
                f = self.image_fixture(calls)
                m = {'id': '1', 'channel_id': 'c', 'author': {'id': 'u'}, 'attachments': [photo()]}
                if guild:
                    m.update(guild_id=guild, content='<@bot>')
                await f.b.mention(m, 'bot')
                self.assertIn('Recebi a foto', f.outputs[0])
                self.assertFalse(f.calls or calls or f.b.jobs)
                self.assertEqual(f.s.count('u'), 0)

    async def test_plain_text_generation_retains_default_and_edits_honor_explicit_ratio(self):
        calls = []
        banana = self.banana(calls)
        await banana.generate('a beach')
        plain = json.loads(calls[-1][1]['body'])
        self.assertEqual(plain['contents'][0]['parts'], [{'text': 'a beach'}])
        self.assertEqual(plain['generationConfig']['imageConfig']['aspectRatio'], '1:1')
        calls.clear()
        f = self.image_fixture(calls)
        i = with_photo(event(name='imagem', text='Mude o fundo'))
        i['data']['options'].append({'name': 'formato', 'value': '16:9'})
        await f.b.interaction(i, await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        edited = self.assert_edited(f, calls)
        self.assertEqual(edited['generationConfig']['imageConfig']['aspectRatio'], '16:9')

    async def test_invalid_attachment_is_reported_without_model_calls(self):
        for name in ('imagem', 'perguntar'):
            for attachment, expected in ((photo(size=MAX_IMAGE_BYTES + 1), '7 MB'),
                                         (photo(content_type='image/gif'), 'PNG, JPEG ou WebP')):
                with self.subTest(name=name, expected=expected):
                    calls = []
                    f = self.image_fixture(calls)
                    i = with_photo(event(name=name), attachment)
                    await f.b.interaction(i, await self._respond(f))
                    await asyncio.gather(*list(f.b.jobs))
                    self.assertIn(expected, f.outputs[0])
                    self.assertEqual(f.extras[0]['kind'], 'error')
                    self.assertFalse(calls or f.calls or f.b.active)

    async def test_expired_or_invalid_download_never_generates_replacement_image(self):
        for name in ('imagem', 'perguntar'):
            for status, body in ((403, b''), (200, b'<html>not an image</html>')):
                with self.subTest(name=name, status=status):
                    calls = []
                    f = self.image_fixture(calls, status=status, body=body)
                    i = with_photo(event(name=name))
                    await f.b.interaction(i, await self._respond(f))
                    await asyncio.gather(*list(f.b.jobs))
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(f.b.banana.auth.token.called)
                    self.assertNotIn('image', f.extras[0])
                    self.assertIn('foto', f.outputs[0])
                    self.assertNotIn('signed', f.outputs[0])
                    self.assertFalse(f.b.active)

    async def test_actual_download_size_is_enforced_even_when_metadata_lies(self):
        fetch = AsyncMock(return_value=Response(PNG + b'excess'))
        with patch('attachments.MAX_IMAGE_BYTES', len(PNG)):
            with self.assertRaisesRegex(AttachmentError, '7 MB'):
                await download_image(photo(), fetcher=fetch)

    async def test_photo_download_sends_bot_user_agent_through_http_transport(self):
        requests = []

        def open_request(req, **_kwargs):
            requests.append(req)
            stream = io.BytesIO(PNG)
            stream.status = 200
            stream.headers = {'Content-Type': 'image/png'}
            return stream

        async def fetch(url, options):
            return _sync_fetch(url, options)

        opener = SimpleNamespace(open=open_request)
        with patch('net.urllib.request.build_opener', return_value=opener):
            result = await download_image(photo(), fetcher=fetch)
        self.assertEqual(result['data'], PNG)
        self.assertEqual(requests[0].get_header('User-agent'), USER_AGENT)
        self.assertIsNone(requests[0].get_header('Authorization'))
        self.assertIsNone(requests[0].get_header('X-goog-user-project'))
        self.assertEqual(requests[0].full_url, photo()['url'])

    async def test_cancel_during_photo_download_stops_before_google_request(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetch(_url, _opts):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        auth = SimpleNamespace(token=AsyncMock(return_value='google-token'))
        banana = Banana(cfg(), auth, fetch)
        signal = AbortSignal()
        task = asyncio.create_task(banana.generate('Edit', signal=signal, attachment=photo()))
        await asyncio.wait_for(started.wait(), 1)
        signal.abort()
        with self.assertRaises(AbortError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        auth.token.assert_not_called()

    async def test_untrusted_urls_are_rejected_before_any_download(self):
        for url in ('http://cdn.discordapp.com/attachments/1/2/a.png',
                    'https://cdn.discordapp.com.evil.test/attachments/1/2/a.png',
                    'https://cdn.discordapp.com@127.0.0.1/attachments/1/2/a.png',
                    'https://cdn.discordapp.com:8443/attachments/1/2/a.png',
                    'https://cdn.discordapp.com/not-attachments/a.png',
                    'file:///tmp/a.png', 'https://127.0.0.1/a.png'):
            with self.subTest(url=url):
                fetch = AsyncMock()
                with self.assertRaises(AttachmentError):
                    await download_image(photo(url=url), fetcher=fetch)
                fetch.assert_not_called()

    def test_only_explicit_attachment_is_used_and_current_message_wins(self):
        original = photo(id='444')
        m = {'channel_id': 'c', 'attachments': [photo()],
             'referenced_message': {'attachments': [original]}}
        self.assertEqual(image_attachment(m)['id'], '333')
        m['attachments'] = []
        self.assertEqual(image_attachment(m)['id'], '444')
        m['message_reference'] = {'channel_id': 'other-channel'}
        self.assertIsNone(image_attachment(m))
        i = with_photo(event())
        i['data']['resolved']['attachments']['444'] = original
        self.assertEqual(image_attachment(i)['id'], '333')
        del i['data']['resolved']['attachments']['333']
        with self.assertRaises(AttachmentError):
            image_attachment(i)
        with self.assertRaisesRegex(AttachmentError, 'uma foto'):
            image_attachment({'attachments': [photo(), original]})

    async def test_supported_file_signatures_and_missing_content_type(self):
        for mime, body in (('image/png', PNG), ('image/jpeg', b'\xff\xd8\xfftest'),
                           ('image/webp', b'RIFF1234WEBPtest')):
            with self.subTest(mime=mime):
                result = await download_image(photo(content_type=mime), fetcher=AsyncMock(return_value=Response(body)))
                self.assertEqual(result, {'data': body, 'mime': mime})
        result = await download_image(photo(content_type=None), fetcher=AsyncMock(return_value=Response(PNG)))
        self.assertEqual(result['mime'], 'image/png')

    def test_slash_photo_option_is_optional_and_available_in_dm(self):
        for command in commands:
            if command['name'] in ('imagem', 'perguntar'):
                option = next(o for o in command['options'] if o['name'] == 'foto')
                self.assertEqual(option['type'], 11)
                self.assertFalse(option.get('required'))
                self.assertIn(1, command['contexts'])


if __name__ == '__main__':
    unittest.main()
