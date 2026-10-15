import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test import test_core
from test.test_core import cfg, event
from bot import Bot, response_text, scope
from config import config
from discord import Discord, commands, utf16_len
from lyria import Lyria, MusicError
from net import AbortError, AbortSignal, FormData, Response


MP3 = b'ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00' + b'\x00' * 400
PROMPT = 'Pop animado em português sobre uma viagem com amigos, com voz feminina'


def music_event(**changes):
    i = event(name='lyra', **changes)
    i['data']['options'] = [{'name': 'prompt', 'type': 3, 'value': PROMPT}]
    return i


def music_response(**changes):
    return {'status': 'completed', 'outputs': [
        {'type': 'text', 'text': 'Letra e descrição'},
        {'type': 'audio', 'mime_type': 'audio/mpeg', 'data': base64.b64encode(MP3).decode()},
    ], **changes}


class MusicTest(unittest.IsolatedAsyncioTestCase):
    fixture = test_core.CoreTest.fixture
    _respond = test_core.CoreTest._respond

    def provider(self, fetch=None):
        return Lyria(cfg(), SimpleNamespace(token=AsyncMock(return_value='google-secret')),
                     fetch or AsyncMock(return_value=Response.json(music_response())))

    def test_command_is_global_with_required_prompt_and_private_option(self):
        command = next(c for c in commands if c['name'] == 'lyra')
        self.assertEqual(command['contexts'], [0, 1, 2])
        self.assertEqual(command['integration_types'], [0, 1])
        self.assertTrue(command['dm_permission'])
        options = {o['name']: o for o in command['options']}
        self.assertEqual(options['prompt']['type'], 3)
        self.assertTrue(options['prompt']['required'])
        self.assertEqual(options['prompt']['max_length'], 4000)
        self.assertEqual(options['privado']['type'], 5)

    def test_config_validates_supported_models_region_and_deadline(self):
        settings = cfg()
        self.assertTrue(settings.enableMusic)
        self.assertEqual(settings.musicModel, 'lyria-3-pro-preview')
        self.assertEqual(settings.musicTimeout, 300000)
        self.assertEqual(settings.musicEndpoint, 'https://aiplatform.googleapis.com/v1beta1/projects/laynos/locations/global/interactions')
        env = {'DISCORD_BOT_TOKEN': 'test', 'DISCORD_APPLICATION_ID': settings.appId, 'GOOGLE_CLOUD_PROJECT': 'laynos'}
        clip = config({**env, 'MUSIC_MODEL': 'lyria-3-clip-preview', 'ENABLE_MUSIC': 'false'})
        self.assertEqual(clip.musicModel, 'lyria-3-clip-preview')
        self.assertFalse(clip.enableMusic)
        for bad in ({'MUSIC_MODEL': '../model'}, {'MUSIC_LOCATION': 'us-central1'},
                    {'MUSIC_TIMEOUT_SECONDS': '601'}, {'ENABLE_MUSIC': 'maybe'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config({**env, **bad})

    async def test_vertex_contract_sends_prompt_and_decodes_mp3_in_both_schemas(self):
        for shape in ('outputs', 'steps'):
            with self.subTest(shape=shape):
                response = music_response()
                if shape == 'steps':
                    response['steps'] = [{'type': 'model_output', 'content': response.pop('outputs')}]
                fetch = AsyncMock(return_value=Response.json(response))
                provider = self.provider(fetch)
                audio = await provider.generate(PROMPT)
                url, options = fetch.call_args.args
                self.assertEqual(url, provider.cfg.musicEndpoint)
                self.assertEqual(options['method'], 'POST')
                self.assertEqual(options['headers']['authorization'], 'Bearer google-secret')
                self.assertEqual(options['headers']['x-goog-user-project'], 'laynos')
                self.assertEqual(json.loads(options['body']), {
                    'model': 'lyria-3-pro-preview', 'input': [{'type': 'text', 'text': PROMPT}]})
                self.assertEqual(options['redirect'], 'error')
                self.assertEqual(audio.data, MP3)
                self.assertEqual((audio.mime, audio.filename), ('audio/mpeg', 'musica.mp3'))

    async def test_slash_delivers_mp3_in_guild_dm_and_user_installation_public_or_private(self):
        for context in (0, 1, 2):
            for private in (False, True):
                with self.subTest(context=context, private=private):
                    f = self.fixture()
                    provider = self.provider()
                    f.b.lyria = provider
                    i = music_event()
                    i['context'] = context
                    if context != 0:
                        i.pop('guild_id')
                        i.pop('member')
                        i['user'] = {'id': 'u'}
                    i['data']['options'].append({'name': 'privado', 'value': private})
                    upload = AsyncMock(return_value=Response.json({'id': 'audio-output'}))
                    delivery = Discord('discord-token', upload)
                    f.b.discord.edit = delivery.edit
                    await f.b.interaction(i, await self._respond(f))
                    self.assertEqual(f.responses, [{'type': 5, 'data': {'flags': 64 if private else 0}}])
                    await asyncio.gather(*list(f.b.jobs))
                    form = upload.call_args.args[1]['body']
                    self.assertIsInstance(form, FormData)
                    self.assertEqual(form.get('files[0]').data, MP3)
                    self.assertEqual(form.get('files[0]').type, 'audio/mpeg')
                    payload = json.loads(form.get('payload_json'))
                    self.assertEqual(payload['attachments'], [{'id': 0, 'filename': 'musica.mp3'}])
                    self.assertEqual(payload['embeds'], [])
                    self.assertEqual(payload['allowed_mentions']['parse'], [])
                    self.assertFalse(f.b.active or f.calls)
                    self.assertEqual(f.s.count('u'), 1)
                    for visibility in ('private', 'public'):
                        self.assertFalse(f.s.history(f'{scope(i)}:{visibility}', f.c.ttl))

    async def test_disabled_music_does_not_use_quota_or_provider(self):
        f = self.fixture()
        f.c.enableMusic = False
        provider = self.provider()
        f.b = Bot(f.c, f.s, f.b.vertex, f.b.discord, lyria=provider)
        await f.b.interaction(music_event(), await self._respond(f))
        self.assertIn('indisponível', response_text(f.responses[0]))
        self.assertFalse(f.b.jobs or f.b.active)
        self.assertEqual(f.s.count('u'), 0)
        provider.auth.token.assert_not_called()

    async def test_reservation_checks_invalid_prompt_quota_and_authorization(self):
        for reason in ('empty', 'long', 'quota', 'unauthorized', 'busy'):
            with self.subTest(reason=reason):
                f = self.fixture()
                f.b.lyria = self.provider()
                i = music_event()
                if reason in ('empty', 'long'):
                    i['data']['options'][0]['value'] = ' ' if reason == 'empty' else 'x' * 4001
                elif reason == 'quota':
                    f.c.userDaily = 1
                    f.s.consume('u', f.c)
                elif reason == 'unauthorized':
                    f.c.allowUsers = {'someone-else'}
                else:
                    f.b.active.add(scope(i))
                await f.b.interaction(i, await self._respond(f))
                self.assertEqual(f.responses[0]['type'], 4)
                self.assertFalse(f.b.jobs)
                f.b.lyria.auth.token.assert_not_called()

    async def test_failed_ack_releases_slot_without_generating_music(self):
        f = self.fixture()
        f.b.lyria = self.provider()
        with self.assertRaises(OSError):
            await f.b.interaction(music_event(), AsyncMock(side_effect=OSError('network')))
        self.assertFalse(f.b.active or f.b.jobs)
        f.b.lyria.auth.token.assert_not_called()

    async def test_cancel_interrupts_generation_and_releases_slot(self):
        f = self.fixture()
        started = asyncio.Event()
        signals = []
        async def generate(prompt, signal):
            signals.append(signal)
            started.set()
            await asyncio.Future()
        f.b.lyria = SimpleNamespace(generate=generate)
        await f.b.interaction(music_event(), await self._respond(f))
        await asyncio.wait_for(started.wait(), timeout=1)
        jobs = list(f.b.jobs)
        await f.b.interaction(event(id='cancel', name='cancelar'), await self._respond(f))
        await asyncio.gather(*jobs, return_exceptions=True)
        self.assertTrue(signals[0].aborted)
        self.assertFalse(f.b.active)
        self.assertIn('interrompida', f.outputs[0])
        self.assertNotIn('audio', f.extras[0])

    async def test_music_deadline_expires_and_releases_slot(self):
        f = self.fixture()
        f.c.musicTimeout = 10
        async def generate(*args):
            await asyncio.Future()
        f.b.lyria = SimpleNamespace(generate=generate)
        await f.b.interaction(music_event(), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertFalse(f.b.active)
        self.assertIn('demorou demais', f.outputs[0])
        self.assertNotIn('audio', f.extras[0])

    async def test_upstream_errors_are_safe_and_generation_is_not_retried(self):
        for status, expected in ((429, 'limite'), (403, 'acesso'), (404, 'acesso'), (503, 'Não consegui')):
            with self.subTest(status=status):
                f = self.fixture()
                fetch = AsyncMock(return_value=Response('secret provider body', status=status))
                f.b.lyria = self.provider(fetch)
                await f.b.interaction(music_event(), await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assertEqual(fetch.await_count, 1)
                self.assertIn(expected, f.outputs[0])
                self.assertNotIn('secret', f.outputs[0])
                self.assertFalse(f.b.active)

    async def test_invalid_audio_is_not_uploaded_and_external_urls_are_not_fetched(self):
        bad = [
            music_response(status='failed'), music_response(outputs=[]), [],
            music_response(outputs=[{'type': 'text', 'text': 'secret provider text'}]),
        ]
        for change in ({'data': '!!!'}, {'data': ''}, {'data': base64.b64encode(b'<html>oops</html>').decode()},
                       {'mime_type': 'video/mp4'}, {'data': None, 'uri': 'https://example.com/private'}):
            part = {**music_response()['outputs'][1], **change}
            bad.append(music_response(outputs=[part]))
        for response in bad:
            with self.subTest(response=response):
                f = self.fixture()
                fetch = AsyncMock(return_value=Response.json(response))
                f.b.lyria = self.provider(fetch)
                await f.b.interaction(music_event(), await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assertEqual(fetch.await_count, 1)
                self.assertEqual(f.extras[0]['kind'], 'error')
                self.assertNotIn('audio', f.extras[0])
                self.assertNotIn('secret', f.outputs[0])
                self.assertFalse(f.b.active)

    async def test_audio_and_response_size_limits(self):
        for limit in (len(MP3) - 1, len(MP3) - 4):
            with self.subTest(limit=limit), patch('lyria.MAX_AUDIO_BYTES', limit):
                with self.assertRaisesRegex(MusicError, 'grande demais'):
                    await self.provider().generate(PROMPT)
        with patch('lyria.JSON_LIMIT', 10):
            with self.assertRaisesRegex(RuntimeError, 'excede'):
                await self.provider().generate(PROMPT)

    async def test_cancellation_during_auth_does_not_send_generation(self):
        started = asyncio.Event()
        async def token():
            started.set()
            await asyncio.Future()
        provider = self.provider()
        provider.auth.token = token
        signal = AbortSignal()
        job = asyncio.create_task(provider.generate(PROMPT, signal))
        await asyncio.wait_for(started.wait(), timeout=1)
        signal.abort()
        with self.assertRaises(AbortError):
            await job
        provider.fetcher.assert_not_called()

    async def test_long_audio_caption_retains_download_and_stays_under_content_limit(self):
        upload = AsyncMock(return_value=Response.json({'id': 'audio'}))
        audio = await self.provider().generate(PROMPT)
        text = '🎵' * 2500
        await Discord('token', upload).output('/test', 'POST', text, audio=audio)
        form = upload.call_args.args[1]['body']
        payload = json.loads(form.get('payload_json'))
        self.assertLessEqual(utf16_len(payload['content']), 2000)
        self.assertEqual(payload['embeds'], [])
        self.assertEqual(form.get('files[0]').data, text)
        self.assertEqual(form.get('files[1]').data, MP3)

    async def test_help_and_status_include_music(self):
        f = self.fixture()
        f.b.lyria = self.provider()
        await f.b.interaction(event(id='help', name='ajuda'), await self._respond(f))
        await f.b.interaction(event(id='status', name='status'), await self._respond(f))
        self.assertIn('/lyra', json.dumps(f.responses[0], ensure_ascii=False))
        fields = f.responses[1]['data']['embeds'][0]['fields']
        self.assertEqual(next(v['value'] for v in fields if v['name'] == 'Músicas'), 'Disponíveis')

    async def test_delivery_failure_does_not_generate_music_twice(self):
        f = self.fixture()
        f.b.lyria = self.provider()
        f.b.discord.edit = AsyncMock(side_effect=[OSError('network'), None])
        await f.b.interaction(music_event(), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(f.b.lyria.fetcher.await_count, 1)
        self.assertIn('música foi gerada', f.b.discord.edit.call_args.args[1])
        self.assertFalse(f.b.active)
