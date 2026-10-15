import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from actions import Actions
from banana import Banana
from bot import Bot, addressed_prompt, mentionPrompt, response_text, scope
from config import config
from discord import Discord, page_row
from guild import member_is_admin, parse_hex
from net import FormData, Response
from store import Store, context
from vertex import Vertex
from net import request


def cfg():
    return config({
        'DISCORD_BOT_TOKEN': 'test-token',
        'DISCORD_APPLICATION_ID': '12345678901234567',
        'GOOGLE_CLOUD_PROJECT': 'laynos',
        'USER_COOLDOWN_SECONDS': '0',
    })


def event(id='1', name='perguntar', text='Olá'):
    return {
        'id': id,
        'application_id': '12345678901234567',
        'token': 'interaction-token',
        'type': 2,
        'guild_id': 'g',
        'channel_id': 'c',
        'member': {'user': {'id': 'u'}},
        'data': {'name': name, 'options': [{'name': 'texto', 'value': text}]},
    }


class CoreTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, complete=None, banana=None, search=None):
        if complete is None:
            async def complete(*_args, **_kwargs):
                return {'text': 'Resposta'}
        c = cfg()
        s = Store(':memory:')
        self.addCleanup(s.close)
        outputs, responses, calls, extras = [], [], [], []

        async def edit(_i, text, **kw):
            outputs.append(text)
            extras.append(kw)

        async def reply(_i, text, **kw):
            outputs.append(text)
            extras.append(kw)

        async def api(*_a, **_k):
            return None

        discord = SimpleNamespace(edit=edit, reply=reply, api=api, me=None)

        async def wrapped(messages, *_a, **_k):
            calls.append(messages)
            return await complete(messages)

        b = Bot(c, s, SimpleNamespace(complete=wrapped), discord, banana=banana, search=search)
        return SimpleNamespace(c=c, s=s, b=b, outputs=outputs, responses=responses, calls=calls, extras=extras)

    async def _respond(self, f):
        async def respond(r):
            f.responses.append(r)
        return respond

    def test_config_rejects_malformed_ids_and_limits(self):
        with self.assertRaises(Exception):
            config({})
        with self.assertRaises(Exception):
            config({
                'DISCORD_BOT_TOKEN': 'x',
                'DISCORD_APPLICATION_ID': '12345678901234567',
                'GOOGLE_CLOUD_PROJECT': 'laynos',
                'MAX_CONCURRENT': '0',
            })
        self.assertRegex(cfg().endpoint, r'https://aiplatform.googleapis.com/.+/locations/global/')
        self.assertRegex(cfg().imageEndpoint, r'publishers/google/models/.+:generateContent')
        self.assertTrue(cfg().enableSearch)
        self.assertRegex(cfg().searchEndpoint, r'publishers/google/models/gemini-2.5-flash:generateContent')

    def test_perguntar_and_video_are_registered_for_dms(self):
        from discord import commands
        by_name = {c['name']: c for c in commands}
        for name in ('perguntar', 'video', 'limpar', 'ajuda', 'status'):
            self.assertIn(0, by_name[name]['contexts'])
            self.assertIn(1, by_name[name]['contexts'])
            self.assertTrue(by_name[name]['dm_permission'])
        for name in ('cor', 'canal'):
            self.assertEqual(by_name[name]['contexts'], [0])
            self.assertFalse(by_name[name]['dm_permission'])

    def test_mention_parsing_accepts_both_forms_and_requires_direct_mention(self):
        self.assertEqual(mentionPrompt('oi <@123> tudo bem?', '123'), 'oi  tudo bem?')
        self.assertEqual(mentionPrompt('<@!123> oi', '123'), 'oi')
        self.assertEqual(mentionPrompt('<@1234> oi', '123'), None)
        self.assertEqual(addressed_prompt({
            'content': 'continua essa ideia',
            'referenced_message': {'author': {'id': '123'}},
        }, '123'), 'continua essa ideia')
        self.assertIsNone(addressed_prompt({
            'content': 'oi',
            'referenced_message': {'author': {'id': '999'}},
        }, '123'))

    def test_persistent_history_ttl_clear_and_idempotency(self):
        dir_ = tempfile.mkdtemp(prefix='laynai-test-')
        path = str(Path(dir_) / 'db.sqlite')
        try:
            s = Store(path)
            s.save('a', [{'role': 'user', 'content': 'x'}], 1000)
            s.close()
            s = Store(path)
            self.assertEqual(len(s.history('a', 2000, 1500)), 1)
            self.assertEqual(s.history('a', 2000, 4000), [])
            self.assertEqual(s.claim('1', 1000), True)
            self.assertEqual(s.claim('1', 1001), False)
            s.clear('a')
            self.assertEqual(s.history('a', 2000, 1500), [])
            s.cleanup(1, 9999999)
            self.assertEqual(s.claim('1'), True)
            page_id = s.save_pages('resposta', '• fonte — https://exemplo.com', 1000)
            self.assertEqual(s.pages(page_id)['answer'], 'resposta')
            s.cleanup(1, 1000 + 7 * 86400000 + 1)
            self.assertIsNone(s.pages(page_id))
            s.close()
        finally:
            shutil.rmtree(dir_)

    def test_quota_is_transactional_global_and_resets_by_utc_date(self):
        s = Store(':memory:')
        self.addCleanup(s.close)
        c = cfg()
        c.userDaily = 1
        c.globalDaily = 2
        self.assertEqual(s.consume('a', c, 1000), None)
        self.assertRegex(s.consume('a', c, 2000), r'limite diário')
        self.assertEqual(s.consume('b', c, 2000), None)
        self.assertRegex(s.consume('c', c, 2000), r'global')
        self.assertEqual(s.consume('a', c, 86400001), None)

    def test_cooldown_enforced_across_channels(self):
        s = Store(':memory:')
        self.addCleanup(s.close)
        c = cfg()
        c.cooldown = 10
        s.consume('u', c, 1000)
        self.assertRegex(s.consume('u', c, 2000), r'Aguarde')

    def test_context_drops_complete_turns_to_bound_size(self):
        c = cfg()
        c.contextChars = 500
        c.system = 'sys'
        out = context([{'role': 'user', 'content': 'a' * 500}, {'role': 'assistant', 'content': 'b'}], 'now', c)
        self.assertEqual([m['role'] for m in out], ['system', 'user'])

    def test_context_tells_the_model_the_real_date_and_time(self):
        from clock import BRASILIA, clock_line, ground_query
        when = datetime(2026, 10, 9, 5, 30, tzinfo=timezone.utc)
        out = context([], 'que horas são?', cfg(), now=when)
        self.assertEqual([m['role'] for m in out], ['system', 'user'])
        system = out[0]['content']
        self.assertIn(clock_line(when), system)
        self.assertIn('Ano 2026', system)
        self.assertIn('mês 10 (outubro)', system)
        self.assertIn('dia 9', system)
        self.assertIn('hora 2', system)
        self.assertIn('minuto 30', system)
        self.assertIn('02:30', system)
        self.assertNotIn('DATA E HORA', out[-1]['content'])
        self.assertEqual(clock_line(datetime(2026, 10, 9, 2, 30, tzinfo=timezone.utc)),
                         'quinta-feira, 8 de outubro de 2026, 23:30 (horário de Brasília)')
        local = datetime(2026, 10, 9, 2, 30, tzinfo=BRASILIA)
        self.assertIn('9 de outubro de 2026', clock_line(local))
        self.assertEqual(ground_query('notícias de hoje', local), 'notícias de hoje — hoje é 9 de outubro de 2026')
        self.assertEqual(ground_query('eleição 2022', local), 'eleição 2022')
        self.assertEqual(ground_query('receita de bolo', local), 'receita de bolo')

    async def test_slash_defers_privately_saves_history_suppresses_duplicates(self):
        f = self.fixture()
        respond = await self._respond(f)
        await f.b.interaction(event(), respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(f.responses[0]['type'], 5)
        self.assertEqual(f.responses[0]['data'].get('flags', 0), 0)
        self.assertEqual(f.outputs, ['Resposta'])
        self.assertNotIn('components', f.extras[0])
        self.assertEqual(len(f.s.history('g:c:u:public', f.c.ttl)), 2)
        await f.b.interaction(event(), respond)
        self.assertEqual(len(f.calls), 1)

    async def test_private_context_never_leaks_into_public_mention(self):
        f = self.fixture()
        respond = await self._respond(f)
        private_text = 'conteudo-privado-sentinela-7c9e'
        secret = event('1', 'perguntar', private_text)
        secret['data']['options'].append({'name': 'privado', 'value': True})
        await f.b.interaction(secret, respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        m = {'id': '2', 'guild_id': 'g', 'channel_id': 'c', 'author': {'id': 'u'}, 'content': '<@123> oi'}
        await f.b.mention(m, '123')
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertNotIn(private_text, json.dumps(f.calls[1], separators=(',', ':')))
        await f.b.mention({**m, 'id': '3', 'content': '<@123> continue'}, '123')
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(f.calls[2]), 4)

    async def test_other_users_and_channels_cannot_see_history(self):
        f = self.fixture()
        respond = await self._respond(f)
        await f.b.interaction(event(), respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        e = event('2')
        e['member']['user']['id'] = 'v'
        await f.b.interaction(e, respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(f.calls[1]), 2)
        self.assertNotEqual(scope(e), scope(event()))

    async def test_allowlists_block_inference_ignores_bots_and_unmentioned(self):
        f = self.fixture()
        respond = await self._respond(f)
        f.c.allowGuilds.add('other')
        await f.b.interaction(event(), respond)
        self.assertEqual(len(f.calls), 0)
        self.assertRegex(response_text(f.responses[0]), r'autorizado')
        f.c.allowGuilds.clear()
        await f.b.mention({'id': 'x', 'guild_id': 'g', 'channel_id': 'c', 'author': {'id': 'u', 'bot': True}, 'content': '<@123> oi'}, '123')
        await f.b.mention({'id': 'y', 'guild_id': 'g', 'channel_id': 'c', 'author': {'id': 'u'}, 'content': 'oi'}, '123')
        self.assertEqual(len(f.calls), 0)
        await f.b.mention({
            'id': 'z', 'guild_id': 'g', 'channel_id': 'c', 'author': {'id': 'u'},
            'content': 'continua',
            'referenced_message': {'id': 'botmsg', 'author': {'id': '123'}},
        }, '123')
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(f.calls[0][-1]['content'], 'continua')

    async def test_concurrency_guard_prevents_overlapping_history_and_clear(self):
        loop = asyncio.get_running_loop()
        held = loop.create_future()

        async def complete(*_a, **_k):
            return await held

        f = self.fixture(complete)
        respond = await self._respond(f)
        await f.b.interaction(event('1'), respond)
        await asyncio.sleep(0)
        await f.b.interaction(event('2'), respond)
        await f.b.interaction(event('3', 'limpar'), respond)
        self.assertEqual(len(f.calls), 1)
        self.assertRegex(response_text(f.responses[1]), r'anterior')
        self.assertRegex(response_text(f.responses[2]), r'Aguarde')
        held.set_result({'text': 'ok'})
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(f.b.active), 0)

    async def test_clear_removes_both_visibility_histories(self):
        f = self.fixture()
        respond = await self._respond(f)
        f.s.save('g:c:u:private', [])
        f.s.save('g:c:u:public', [])
        await f.b.interaction(event('1', 'limpar'), respond)
        n = f.s.db.execute('SELECT count(*) AS n FROM sessions').fetchone()['n']
        self.assertEqual(n, 0)

    async def test_upstream_failure_does_not_poison_history_or_leave_a_lock(self):
        async def complete(*_a, **_k):
            err = RuntimeError('secret token')
            err.status = 403
            raise err

        f = self.fixture(complete)
        respond = await self._respond(f)
        await f.b.interaction(event(), respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(f.b.active), 0)
        self.assertEqual(f.s.history('g:c:u:private', f.c.ttl), [])
        self.assertNotIn('secret', f.outputs[0])
        self.assertRegex(f.outputs[0], r'administrador')

    async def test_failed_acknowledgement_releases_reservation(self):
        f = self.fixture()

        async def boom(_r):
            raise RuntimeError('offline')

        with self.assertRaises(RuntimeError):
            await f.b.interaction(event(), boom)
        self.assertEqual(len(f.b.active), 0)
        self.assertEqual(len(f.calls), 0)

    async def test_discord_output_blocks_pings_and_sends_long_unicode_as_attachment(self):
        seen = []

        async def fetcher(_url, opts):
            seen.append(opts)
            return Response.json({'id': '1'})

        d = Discord('token', fetcher)
        await d.output('/test', 'POST', 'só a resposta', components=page_row('ab' * 8, 0))
        paged = json.loads(seen[0]['body'])
        self.assertEqual(paged['embeds'][0]['description'], 'só a resposta')
        self.assertEqual(paged['components'][0]['components'][0]['emoji']['name'], '⬅️')
        self.assertTrue(paged['components'][0]['components'][0]['disabled'])
        self.assertFalse(paged['components'][0]['components'][1]['disabled'])
        self.assertNotIn('Fontes', paged['embeds'][0]['description'])
        await d.output('/test', 'POST', '@everyone')
        body = json.loads(seen[1]['body'])
        self.assertEqual(body['allowed_mentions']['parse'], [])
        self.assertEqual(body['embeds'][0]['description'], '@everyone')
        await d.output('/test', 'POST', '😀' * 5000)
        self.assertIsInstance(seen[2]['body'], FormData)
        self.assertEqual(await seen[2]['body'].get('files[0]').text(), '😀' * 5000)

    async def test_vertex_uses_correct_endpoint_bearer_token_and_text_response(self):
        sent = {}

        async def fetcher(url, o):
            sent.clear()
            sent.update({'url': url, **o})
            return Response.json({'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'length'}]})

        async def token():
            return 'abc'

        v = Vertex(cfg(), SimpleNamespace(token=token), fetcher)
        from net import AbortSignal
        r = await v.complete([{'role': 'user', 'content': 'hi'}], AbortSignal.timeout(1000))
        self.assertEqual(r.truncated, True)
        self.assertEqual(sent['headers']['authorization'], 'Bearer abc')
        sent_body = json.loads(sent['body'])
        self.assertEqual(sent_body['model'], 'google/gemini-4-argon')
        self.assertEqual(sent_body['reasoning_effort'], 'low')
        self.assertNotIn('temperature', sent_body)
        self.assertEqual(sent_body['tools'][0]['function']['name'], 'generate_image')
        names = [t['function']['name'] for t in sent_body['tools']]
        self.assertIn('generate_video', names)
        self.assertIn('web_search', names)

    async def test_gemini_tool_round_preserves_signatures_without_exposing_or_storing_them(self):
        from unittest.mock import AsyncMock
        signature = {'google': {'thought_signature': 'opaque-call-signature'}}
        message_signature = {'google': {'thought_signature': 'opaque-message-signature'}}
        calls = []
        async def fetcher(_url, opts):
            payload = json.loads(opts['body'])
            calls.append(payload)
            if len(calls) == 1:
                return Response.json({'choices': [{'message': {
                    'role': 'assistant', 'content': None, 'extra_content': message_signature,
                    'tool_calls': [{'id': 'search-call', 'type': 'function', 'extra_content': signature,
                                    'function': {'name': 'web_search', 'arguments': '{"query":"teste"}'}}],
                }, 'finish_reason': 'tool_calls'}]})
            assistant, result = payload['messages'][-2:]
            self.assertEqual(assistant['extra_content'], message_signature)
            self.assertEqual(assistant['tool_calls'][0]['extra_content'], signature)
            self.assertEqual(result['tool_call_id'], 'search-call')
            return Response.json({'choices': [{'message': {'content': 'Consulta concluída'}, 'finish_reason': 'stop'}]})
        search = SimpleNamespace(search=AsyncMock(return_value={'query': 'teste', 'answer': 'OK', 'sources': []}))
        f = self.fixture(search=search)
        f.b.vertex = Vertex(f.c, SimpleNamespace(token=AsyncMock(return_value='token')), fetcher)
        logs = []
        f.b.log = lambda event, **fields: logs.append((event, fields))
        i = event(text='Pesquise teste')
        await f.b.interaction(i, await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(len(calls), 2)
        self.assertIn('Consulta concluída', f.outputs[0])
        saved = f.s.history(f'{scope(i)}:public', f.c.ttl)
        for value in (f.outputs, logs, saved):
            self.assertNotIn('opaque-', json.dumps(value))
        self.assertFalse(f.b.active)

    async def test_argon_reasoning_setting_and_other_model_temperature_are_separate(self):
        from unittest.mock import AsyncMock
        for model, effort in (('google/gemini-4-argon', 'high'), ('publisher/model', 'low')):
            with self.subTest(model=model):
                c = cfg()
                c.model, c.reasoningEffort = model, effort
                fetch = AsyncMock(return_value=Response.json({'choices': [{'message': {'content': 'OK'}}]}))
                await Vertex(c, SimpleNamespace(token=AsyncMock(return_value='token')), fetch).complete(
                    [{'role': 'user', 'content': 'Olá'}], tools=[])
                payload = json.loads(fetch.call_args.args[1]['body'])
                if model == 'google/gemini-4-argon':
                    self.assertEqual(payload['reasoning_effort'], effort)
                    self.assertNotIn('temperature', payload)
                else:
                    self.assertEqual(payload['temperature'], c.temperature)
                    self.assertNotIn('reasoning_effort', payload)
                self.assertNotIn('tools', payload)

    async def test_banana_decodes_inline_image_from_vertex(self):
        sent = {}

        async def fetcher(url, o):
            sent.clear()
            sent.update({'url': url, **o})
            return Response.json({'candidates': [{'content': {'parts': [
                {'text': 'ok'},
                {'inlineData': {'mimeType': 'image/png', 'data': 'aGVsbG8='}},
            ]}}]})

        async def token():
            return 'abc'

        img = await Banana(cfg(), SimpleNamespace(token=token), fetcher).generate('a cat')
        self.assertEqual(img.data, b'hello')
        self.assertIn('generateContent', sent['url'])

    async def test_video_slash_runs_in_dm(self):
        f = self.fixture()
        generated = []

        async def generate(prompt, aspect, seconds, _signal):
            generated.append((prompt, aspect, seconds))
            return SimpleNamespace(data=b'vid', mime='video/mp4', filename='video.mp4', caption='clipe')

        f.b.omni = SimpleNamespace(generate=generate)
        dm = event('9', 'video', 'gato surfando')
        dm.pop('guild_id')
        dm.pop('member')
        dm['user'] = {'id': 'u'}
        respond = await self._respond(f)
        await f.b.interaction(dm, respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(f.responses[0]['type'], 5)
        self.assertEqual(generated, [('gato surfando', '16:9', 6)])
        self.assertEqual(f.outputs, ['clipe'])
        self.assertEqual(f.extras[0]['kind'], 'video')
        self.assertEqual(scope(dm), 'dm:c:u')

    async def test_omni_decodes_video_from_interaction(self):
        from omni import Omni
        sent = {}

        async def fetcher(url, o):
            sent.clear()
            sent.update({'url': url, **o})
            return Response.json({
                'status': 'completed',
                'steps': [{'type': 'model_output', 'content': [
                    {'type': 'video', 'mime_type': 'video/mp4', 'data': 'aGVsbG8='},
                ]}],
            })

        async def token():
            return 'abc'

        clip = await Omni(cfg(), SimpleNamespace(token=token), fetcher).generate('a cat running')
        self.assertEqual(clip.data, b'hello')
        self.assertEqual(clip.filename, 'video.mp4')
        self.assertIn('/interactions', sent['url'])
        sent_body = json.loads(sent['body'])
        self.assertEqual(sent_body['model'], 'gemini-omni-1.1-flash-preview')
        self.assertTrue(sent_body.get('background'))
        self.assertEqual(sent_body['response_format']['duration'], '6s')
        self.assertEqual(sent_body['response_format']['resolution'], '360p')

    async def test_gemini_tool_call_is_handed_to_banana(self):
        n = {'v': 0}

        async def complete(*_a, **_k):
            n['v'] += 1
            if n['v'] == 1:
                return {'text': '', 'tool_calls': [{'function': {
                    'name': 'generate_image',
                    'arguments': json.dumps({'prompt': 'a red fox', 'caption': 'Uma raposa'}),
                }}]}
            return {'text': 'Uma raposa'}

        async def generate(prompt, *_a, **_k):
            self.assertEqual(prompt, 'a red fox')
            return SimpleNamespace(data=b'PNG', mime='image/png', filename='imagem.png', caption='x')

        f = self.fixture(complete, banana=SimpleNamespace(generate=generate))
        respond = await self._respond(f)
        await f.b.interaction(event(), respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertEqual(f.outputs, ['Uma raposa'])
        self.assertEqual(f.extras[0]['kind'], 'image')
        self.assertEqual(f.extras[0]['image'].data, b'PNG')

    async def test_web_search_sends_google_search_tool_and_keeps_sources(self):
        from search import WebSearch, parse_search
        sent = {}

        async def fetcher(url, o):
            sent.clear()
            sent.update({'url': url, **o})
            return Response.json({'candidates': [{
                'content': {'parts': [{'text': 'Amanhã chove.'}]},
                'groundingMetadata': {
                    'webSearchQueries': ['previsão são paulo'],
                    'groundingChunks': [{'web': {'uri': 'https://exemplo.com/tempo', 'title': 'Clima'}}],
                },
            }]})

        async def token():
            return 'abc'

        when = datetime(2026, 10, 9, 15, 4, tzinfo=timezone.utc)
        found = await WebSearch(cfg(), SimpleNamespace(token=token), fetcher).search('previsão de hoje', now=when)
        body = json.loads(sent['body'])
        self.assertEqual(body['tools'], [{'googleSearch': {}}])
        instruction = body['systemInstruction']['parts'][0]['text']
        self.assertIn('9 de outubro de 2026', instruction)
        self.assertIn('12:04', instruction)
        self.assertEqual(body['contents'][0]['parts'][0]['text'], 'previsão de hoje — hoje é 9 de outubro de 2026')
        self.assertEqual(sent['headers']['authorization'], 'Bearer abc')
        self.assertIn('gemini-2.5-flash:generateContent', sent['url'])
        self.assertEqual(found['answer'], 'Amanhã chove.')
        self.assertEqual(found['query'], 'previsão de hoje')
        self.assertEqual(found['sources'], [{'title': 'Clima', 'url': 'https://exemplo.com/tempo'}])
        self.assertEqual(found['searches'], ['previsão são paulo'])
        with self.assertRaises(RuntimeError):
            parse_search('x', {'candidates': [{'content': {'parts': [{'text': 'sem fonte'}]}}]})

    async def test_gemini_cites_sources_from_web_search(self):
        n = {'v': 0}
        seen = {}

        async def complete(messages, *_a, **_k):
            n['v'] += 1
            if n['v'] == 1:
                return {'text': '', 'tool_calls': [{'id': 's1', 'function': {
                    'name': 'web_search',
                    'arguments': json.dumps({'query': 'placar do jogo'}),
                }}]}
            seen['messages'] = messages
            return {'text': 'O jogo terminou 2 a 1.'}

        async def search(query, _signal=None):
            self.assertEqual(query, 'placar do jogo')
            return {
                'query': query,
                'answer': 'Placar 2 a 1.',
                'sources': [{'title': 'GE', 'url': 'https://ge.globo.com/jogo'}],
                'searches': ['placar do jogo'],
            }

        f = self.fixture(complete, search=SimpleNamespace(search=search))
        respond = await self._respond(f)
        await f.b.interaction(event(), respond)
        if f.b.jobs:
            await asyncio.gather(*list(f.b.jobs))
        self.assertIn('2 a 1', f.outputs[0])
        self.assertNotIn('https://ge.globo.com/jogo', f.outputs[0])
        self.assertNotIn('Fontes', f.outputs[0])
        buttons = f.extras[0]['components'][0]['components']
        self.assertEqual([b['emoji']['name'] for b in buttons], ['⬅️', '➡️'])
        self.assertTrue(buttons[0]['disabled'])
        self.assertFalse(buttons[1]['disabled'])
        page_id = buttons[1]['custom_id'].split(':')[1]
        stored = f.s.pages(page_id)
        self.assertEqual(stored['answer'], 'O jogo terminou 2 a 1.')
        self.assertIn('https://ge.globo.com/jogo', stored['sources'])
        self.assertIn('https://www.google.com/search?q=placar+do+jogo', stored['sources'])
        self.assertNotIn('2 a 1', stored['sources'])
        history = f.s.history('g:c:u:public', f.c.ttl)
        self.assertIn('https://ge.globo.com/jogo', history[-1]['content'])

        right = event('page-right')
        right['type'] = 3
        right['data'] = {'custom_id': buttons[1]['custom_id'], 'component_type': 2}
        await f.b.interaction(right, respond)
        opened = f.responses[-1]
        self.assertEqual(opened['type'], 7)
        self.assertEqual(opened['data']['embeds'][0]['title'], 'Fontes')
        self.assertIn('https://ge.globo.com/jogo', opened['data']['embeds'][0]['description'])
        self.assertNotIn('2 a 1', opened['data']['embeds'][0]['description'])
        opened_buttons = opened['data']['components'][0]['components']
        self.assertFalse(opened_buttons[0]['disabled'])
        self.assertTrue(opened_buttons[1]['disabled'])

        left = event('page-left')
        left['type'] = 3
        left['data'] = {'custom_id': buttons[0]['custom_id'], 'component_type': 2}
        await f.b.interaction(left, respond)
        back = f.responses[-1]
        self.assertEqual(back['type'], 7)
        self.assertNotIn('title', back['data']['embeds'][0])
        self.assertIn('2 a 1', back['data']['embeds'][0]['description'])
        self.assertNotIn('ge.globo.com', back['data']['embeds'][0]['description'])
        back_buttons = back['data']['components'][0]['components']
        self.assertTrue(back_buttons[0]['disabled'])
        self.assertFalse(back_buttons[1]['disabled'])
        self.assertEqual(f.s.count('u'), 1)

        f.s.db.execute('DELETE FROM pages')
        gone = event('page-gone')
        gone['type'] = 3
        gone['data'] = {'custom_id': buttons[1]['custom_id'], 'component_type': 2}
        await f.b.interaction(gone, respond)
        self.assertEqual(f.responses[-1]['type'], 4)
        self.assertEqual(f.responses[-1]['data']['flags'], 64)
        self.assertIn('expiraram', f.responses[-1]['data']['embeds'][0]['description'])
        bad = event('page-bad')
        bad['type'] = 3
        bad['data'] = {'custom_id': 'pg:not-valid:1', 'component_type': 2}
        await f.b.interaction(bad, respond)
        self.assertEqual(f.responses[-1]['type'], 4)
        self.assertIn('não está mais disponível', f.responses[-1]['data']['embeds'][0]['description'])
        tool_msgs = [m for m in seen['messages'] if m.get('role') == 'tool']
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn('Placar 2 a 1.', tool_msgs[0]['content'])
        self.assertIn('as_of', tool_msgs[0]['content'])
        self.assertIn('DATA E HORA ATUAIS', seen['messages'][0]['content'])

    def test_channel_name_normalizes_for_lookup(self):
        from actions import _BLANK, _channel_name, _norm_chan
        self.assertEqual(_norm_chan('#Chat Geral'), 'chat-geral')
        self.assertEqual(_norm_chan('chat-geral'), 'chat-geral')
        pretty = _channel_name('🔧 | Teste')
        self.assertEqual(pretty, f'🔧{_BLANK}│{_BLANK}Teste')
        self.assertNotIn('--', pretty)
        self.assertNotIn('|', pretty)
        undone = _channel_name('🔧-│-teste')
        self.assertEqual(undone, f'🔧{_BLANK}│{_BLANK}teste')
        self.assertEqual(_channel_name('Lobby', kind='category'), 'Lobby')
        from actions import _display_name, _norm_chan as norm
        self.assertEqual(_display_name(f'🔧{_BLANK}│{_BLANK}Teste'), '🔧 │ Teste')
        self.assertEqual(norm(f'🔧{_BLANK}│{_BLANK}Teste'), norm('🔧 │ Teste'))

    def test_parse_channel_requests_from_portuguese(self):
        from actions import parse_channel_requests
        names = [(i['kind'], i['name']) for i in parse_channel_requests('cria o canal musica')]
        self.assertIn(('text', 'musica'), names)
        kinds = {i['name']: i['kind'] for i in parse_channel_requests('crie os canais geral, midia e voz')}
        self.assertEqual(kinds.get('geral'), 'text')
        self.assertEqual(kinds.get('voz'), 'voice')

    def test_hex_and_admin_bits(self):
        from guild import member_is_admin, parse_hex
        self.assertEqual(parse_hex('#Ff5500')[0], 'ff5500')
        self.assertEqual(parse_hex('abc')[1], 0xaabbcc)
        self.assertIsNone(parse_hex('zzzzzz'))
        self.assertTrue(member_is_admin('8'))
        self.assertFalse(member_is_admin(32))
        self.assertFalse(member_is_admin(0))

    async def test_color_tag_is_only_applied_to_the_requester(self):
        from actions import Actions
        seen = []

        async def api(path, method='GET', payload=None, multipart=False):
            seen.append((path, method, payload))
            if path.endswith('/members/u') and method == 'GET':
                return {'roles': []}
            if path.endswith('/roles') and method == 'GET':
                return []
            if path.endswith('/roles') and method == 'POST':
                return {'id': 'role1', 'name': payload['name']}
            if method == 'PUT':
                return None
            if method == 'POST' and path.endswith('/channels'):
                return {'id': 'ch1', 'name': payload['name']}
            if method == 'GET' and path.endswith('/channels'):
                return [{'id': 'ch1', 'name': 'geral', 'type': 0}]
            if method == 'PATCH' and '/channels/' in path:
                return {'id': 'ch1', 'name': payload.get('name')}
            return []

        actions = Actions(SimpleNamespace(api=api))
        ev = event()
        out = await actions.run('set_my_color', {'hex': '#ff5500', 'user_id': 'outra-pessoa'}, ev)
        self.assertFalse(out['ok'])
        self.assertEqual(seen, [])
        out = await actions.run('set_my_color', {'hex': '#ff5500'}, ev)
        self.assertTrue(out['ok'])
        self.assertTrue(any(s[1] == 'PUT' and '/members/u/roles/' in s[0] for s in seen))
        self.assertFalse(any('outra-pessoa' in s[0] for s in seen))
        denied = await actions.run('create_channel', {'name': 'geral'}, ev, admin=False)
        self.assertFalse(denied['ok'])
        self.assertRegex(denied['error'], r'administradores')
        created = await actions.run('create_channel', {'name': 'geral'}, ev, admin=True)
        self.assertTrue(created['ok'])
        self.assertEqual(created['name'], 'geral')
        bulk = await actions.run('apply_channel_names', {'renames': [{'channel': 'geral', 'name': '🔧 | Geral'}]}, ev, admin=True)
        self.assertTrue(bulk['ok'])
        self.assertEqual(bulk['done'], 1)

    async def test_http_does_not_retry_authorization_failures_or_network_errors(self):
        n = {'v': 0}

        async def forbidden(_url, _opts):
            n['v'] += 1
            return Response('', {'status': 403})

        with self.assertRaises(Exception):
            await request('https://example.test', {}, fetcher=forbidden)
        self.assertEqual(n['v'], 1)
        n['v'] = 0

        async def offline(_url, _opts):
            n['v'] += 1
            raise RuntimeError('offline')

        with self.assertRaises(Exception):
            await request('https://example.test', {}, fetcher=offline)
        self.assertEqual(n['v'], 1)

    async def test_http_respects_long_retry_after_without_early_retry(self):
        n = {'v': 0}

        async def limited(_url, _opts):
            n['v'] += 1
            return Response('', {'status': 429, 'headers': {'retry-after': '60'}})

        with self.assertRaises(Exception):
            await request('https://example.test', {}, fetcher=limited)
        self.assertEqual(n['v'], 1)


if __name__ == '__main__':
    unittest.main()
