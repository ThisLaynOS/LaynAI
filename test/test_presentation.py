import asyncio
import io
import json
import unittest
import zipfile

from test import test_core
from discord import Discord, text_preview, utf16_len
from net import FormData, Response
from presentation import prepare_response
from types import SimpleNamespace


class PresentationTest(unittest.TestCase):
    def test_source_bytes_unicode_indentation_and_line_endings_are_preserved(self):
        code = '# Olá 🌷\r\ndef oi():\r\n\tprint("ação")  \r\n\r\n'
        raw = 'Exibe uma saudação.\r\n\r\n```python filename=saudacao.py\r\n' + code + '```\r\n\r\nExecute `python saudacao.py`.'
        result = prepare_response(raw)
        self.assertEqual(result.document['filename'], 'saudacao.py')
        self.assertEqual(result.document['data'], code.encode())
        self.assertIn('Execute `python saudacao.py`.', result.text)
        self.assertNotIn('def oi()', result.text)
        self.assertIn(code, result.history)

    def test_language_aliases_get_usable_extensions(self):
        for language, name in [('py', 'codigo.py'), ('javascript', 'codigo.js'), ('tsx', 'codigo.tsx'),
                               ('c++', 'codigo.cpp'), ('c#', 'codigo.cs'), ('luau', 'codigo.luau'),
                               ('dockerfile', 'Dockerfile'), ('json', 'codigo.json')]:
            with self.subTest(language=language):
                result = prepare_response(f'```{language}\ncontent\n```')
                self.assertEqual(result.document['filename'], name)
                self.assertEqual(result.document['data'], b'content\n')

    def test_names_from_headings_and_fences(self):
        for prefix, info, name in [
            ('### `app.py`\n\n', 'python', 'app.py'),
            ('**Arquivo: main.js**\n', 'js', 'main.js'),
            ('', 'python app.py', 'app.py'),
            ('', 'app.py', 'app.py'),
            ('', 'json filename="package.json"', 'package.json'),
            ('', 'text filename=.env.example', '.env.example'),
        ]:
            with self.subTest(info=info, prefix=prefix):
                result = prepare_response(prefix + f'```{info}\ncontent\n```')
                self.assertEqual(result.document['filename'], name)
                self.assertEqual(result.text.count(f'Arquivo: `{name}`'), 1)

    def test_project_archive_preserves_directories_and_usage_instructions(self):
        raw = ('Uma página com estilos.\n\n```html filename=public/index.html\n<h1>Oi</h1>\n```\n\n'
               '```css filename=public/style.css\nh1 { color: red; }\n```\n\nAbra `public/index.html`.')
        result = prepare_response(raw)
        self.assertEqual(result.document['filename'], 'projeto.zip')
        with zipfile.ZipFile(io.BytesIO(result.document['data'])) as bundle:
            self.assertEqual(bundle.read('public/index.html'), b'<h1>Oi</h1>\n')
            self.assertEqual(bundle.read('public/style.css'), b'h1 { color: red; }\n')
            self.assertIn('Abra `public/index.html`.', bundle.read('LEIA-ME-LaynAI.md').decode())
            self.assertEqual(bundle.testzip(), None)
        self.assertNotIn('<h1>', result.text)

    def test_single_nested_file_preserves_its_directory_in_zip(self):
        result = prepare_response('```python filename=src/main.py\npass\n```')
        with zipfile.ZipFile(io.BytesIO(result.document['data'])) as bundle:
            self.assertEqual(bundle.read('src/main.py'), b'pass\n')

    def test_explicitly_named_empty_files_are_preserved(self):
        result = prepare_response('```python filename=src/__init__.py\n```\n```python filename=src/main.py\npass\n```')
        with zipfile.ZipFile(io.BytesIO(result.document['data'])) as bundle:
            self.assertEqual(bundle.read('src/__init__.py'), b'')
            self.assertEqual(bundle.read('src/main.py'), b'pass\n')

    def test_duplicate_names_and_file_directory_collisions_do_not_lose_files(self):
        names = ['app.py', 'APP.py', 'app.py/child.py', 'lib/main.py', 'lib', 'LEIA-ME-LaynAI.md']
        raw = '\n'.join(f'```python filename={name}\n# file {index}\n```' for index, name in enumerate(names))
        result = prepare_response(raw)
        with zipfile.ZipFile(io.BytesIO(result.document['data'])) as bundle:
            archived = bundle.namelist()
            self.assertEqual(len(archived), len(names) + 1)
            self.assertEqual(len(set(name.casefold() for name in archived)), len(archived))
            for index in range(len(names)):
                self.assertIn(f'# file {index}\n'.encode(), [bundle.read(name) for name in archived])
            lowered = [name.casefold() for name in archived]
            for name in lowered:
                self.assertFalse(any(other.startswith(name + '/') for other in lowered))

    def test_model_filenames_cannot_escape_zip_or_inject_multipart_headers(self):
        names = ['../../etc/passwd', '/tmp/absolute.py', 'C:\\temp\\main.py', 'NUL.txt',
                 '../..', 'a`@everyone.py', 'pasta/ação.py']
        raw = '\n'.join(f'```python filename="{name}"\npass\n```' for name in names)
        result = prepare_response(raw)
        with zipfile.ZipFile(io.BytesIO(result.document['data'])) as bundle:
            for name in bundle.namelist():
                self.assertNotIn('..', name.split('/'))
                self.assertFalse(name.startswith('/'))
                self.assertNotRegex(name, r'[\\\r\n"`:@]')
            self.assertIn('_NUL.txt', bundle.namelist())
            self.assertIn('pasta/acao.py', bundle.namelist())

    def test_nested_fences_inside_markdown_are_one_file(self):
        body = '# Exemplo\n\n```python\nprint("oi")\n```\n'
        result = prepare_response('````markdown filename=README.md\n' + body + '````\nPronto.')
        self.assertEqual(result.document['filename'], 'README.md')
        self.assertEqual(result.document['data'].decode(), body)
        self.assertIn('Pronto.', result.text)

    def test_tilde_fences_work_without_swallowing_prose(self):
        raw = '~~~python filename=main.py\nprint("ok")\n~~~\n\nExecute `python main.py`.'
        result = prepare_response(raw)
        self.assertEqual(result.document['data'], b'print("ok")\n')
        self.assertIn('Execute', result.text)

    def test_markdown_fence_indentation_does_not_break_python_file(self):
        raw = '  ```python filename=app.py\n  def oi():\n      print("oi")\n  ```'
        result = prepare_response(raw)
        self.assertEqual(result.document['data'], b'def oi():\n    print("oi")\n')

    def test_short_terminal_commands_logs_and_casual_chat_stay_readable(self):
        for raw in ['Oii! Tudo bem? 🌷', '```sh\npip install flask\npython app.py\n```',
                    '```text\nSyntaxError: invalid syntax\n```', '```\nSaída esperada\n```',
                    '```mermaid\ngraph LR; A-->B\n```', '```python\n\n```']:
            with self.subTest(raw=raw):
                result = prepare_response(raw)
                self.assertIsNone(result.document)
                self.assertEqual(result.text, raw)
        result = prepare_response('```sh filename=start.sh\npython app.py\n```')
        self.assertEqual(result.document['filename'], 'start.sh')

    def test_unclosed_code_is_downloadable_but_marked_partial(self):
        code = 'def oi():\n    print('
        for truncated in (True, False):
            result = prepare_response('```python filename=app.py\n' + code, truncated=truncated)
            self.assertEqual(result.document['filename'], 'app.py.partial')
            self.assertEqual(result.document['data'].decode(), code)
            self.assertIn('Resposta incompleta', result.text)
            self.assertIn('Resposta incompleta', result.history)

    def test_length_limit_marks_project_incomplete_even_after_closed_fences(self):
        raw = '```html filename=index.html\n<h1>Oi</h1>\n```\n```css filename=style.css\nh1 {}\n```'
        result = prepare_response(raw, truncated=True)
        self.assertEqual(result.document['filename'], 'projeto-incompleto.zip')
        self.assertIn('Resposta incompleta', result.text)
        self.assertIn('Resposta incompleta', result.history)

    def test_large_project_uses_one_attachment_and_keeps_every_file(self):
        raw = '\n'.join(f'```python filename=part{i}.py\nvalue = {i}\n```' for i in range(30))
        result = prepare_response(raw)
        with zipfile.ZipFile(io.BytesIO(result.document['data'])) as bundle:
            self.assertEqual(len(bundle.namelist()), 31)
            self.assertEqual(bundle.read('part29.py'), b'value = 29\n')

    def test_long_preview_breaks_at_paragraphs_and_closes_terminal_fences(self):
        text = 'Parágrafo completo.\n\n' + '😀' * 3000
        self.assertLessEqual(utf16_len(text_preview(text)), 4096)
        terminal = 'Comandos:\n```sh\n' + ('echo olá 🌷\n' * 600) + '```'
        preview = text_preview(terminal)
        self.assertLessEqual(utf16_len(preview), 4096)
        self.assertTrue(preview.endswith('```\n\n…'))


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    fixture = test_core.CoreTest.fixture
    _respond = test_core.CoreTest._respond

    async def test_files_are_delivered_in_dm_mentions_slash_and_private_commands(self):
        async def complete(*_args):
            return {'text': 'Um script simples.\n```python filename=app.py\nprint("oi")\n```\nExecute `python app.py`.'}
        for mode in ('slash', 'private', 'dm', 'mention', 'user_install'):
            with self.subTest(mode=mode):
                f = self.fixture(complete)
                if mode in ('dm', 'mention'):
                    msg = {'id': 'm1', 'channel_id': 'c', 'author': {'id': 'u'}, 'content': 'crie um script'}
                    if mode == 'mention':
                        msg.update(guild_id='g', content='<@123> crie um script')
                    await f.b.mention(msg, '123')
                else:
                    interaction = test_core.event(text='crie um script')
                    if mode == 'private':
                        interaction['data']['options'].append({'name': 'privado', 'value': True})
                    if mode == 'user_install':
                        interaction.update(context=0, authorizing_integration_owners={'1': 'u'})
                    await f.b.interaction(interaction, await self._respond(f))
                await asyncio.gather(*list(f.b.jobs))
                self.assertEqual(f.extras[0]['document']['filename'], 'app.py')
                self.assertEqual(f.extras[0]['document']['data'], b'print("oi")\n')
                self.assertNotIn('print("oi")', f.outputs[0])
                key = ('dm' if mode == 'dm' else 'g') + ':c:u:' + ('private' if mode == 'private' else 'public')
                self.assertIn('print("oi")', f.s.history(key, f.c.ttl)[-1]['content'])
                if mode == 'private':
                    self.assertEqual(f.responses[0]['data']['flags'], 64)
                    self.assertEqual(f.s.history('g:c:u:public', f.c.ttl), [])
                self.assertIn('filename=caminho/arquivo.ext', f.calls[0][0]['content'])

    async def test_follow_up_model_receives_original_code(self):
        async def complete(*_args):
            return {'text': '```python filename=main.py\nimportant_value = 42\n```'}
        f = self.fixture(complete)
        for index, prompt in enumerate(('crie um programa', 'mude para 43')):
            await f.b.interaction(test_core.event(str(index), text=prompt), await self._respond(f))
            await asyncio.gather(*list(f.b.jobs))
        history = [message for message in f.calls[1] if message['role'] == 'assistant']
        self.assertIn('important_value = 42', history[0]['content'])

    async def test_follow_up_keeps_large_source_file_with_new_context_budget(self):
        code = 'value = 42\n' * 2800
        async def complete(*_args):
            return {'text': '```python filename=main.py\n' + code + '```'}
        f = self.fixture(complete)
        for index in range(2):
            await f.b.interaction(test_core.event(str(index), text='ajuste o programa'), await self._respond(f))
            await asyncio.gather(*list(f.b.jobs))
        history = [message for message in f.calls[1] if message['role'] == 'assistant']
        self.assertEqual(len(history), 1)
        self.assertIn(code, history[0]['content'])

    async def test_sources_buttons_keep_attachment_and_return_formatted_answer(self):
        count = 0
        async def complete(*_args):
            nonlocal count
            count += 1
            if count == 1:
                return {'tool_calls': [{'id': 's1', 'function': {'name': 'web_search', 'arguments': '{"query":"python docs"}'}}]}
            return {'text': 'Exemplo.\n```python filename=app.py\nprint("oi")\n```'}
        async def search(*_args):
            return {'answer': 'Docs.', 'sources': [{'title': 'Python', 'url': 'https://docs.python.org/3/'}]}
        f = self.fixture(complete, search=SimpleNamespace(search=search))
        respond = await self._respond(f)
        await f.b.interaction(test_core.event(), respond)
        await asyncio.gather(*list(f.b.jobs))
        self.assertIn('document', f.extras[0])
        buttons = f.extras[0]['components'][0]['components']
        for page in (1, 0):
            event = test_core.event('page-' + str(page))
            event.update(type=3, data={'custom_id': buttons[page]['custom_id'], 'component_type': 2})
            await f.b.interaction(event, respond)
            data = f.responses[-1]['data']
            self.assertNotIn('attachments', data)
            if page == 0:
                self.assertEqual(data['embeds'][0]['description'], f.outputs[0])
        history = f.s.history('g:c:u:public', f.c.ttl)[-1]['content']
        self.assertIn('print("oi")', history)
        self.assertIn('https://docs.python.org/3/', history)

    async def test_multipart_has_exact_source_bytes_and_noncolliding_long_text_file(self):
        sent = []
        async def fetch(_url, opts):
            sent.append(opts['body'])
            return Response.json({'id': '1'})
        raw = '😀' * 3000 + '\n\n```text filename=resposta.txt\nconteúdo do arquivo\n```'
        prepared = prepare_response(raw)
        await Discord('token', fetch).output('/test', 'POST', prepared.text, document=prepared.document)
        form = sent[0]
        self.assertIsInstance(form, FormData)
        payload = json.loads(form.get('payload_json'))
        self.assertEqual([file['filename'] for file in payload['attachments']], ['resposta-completa.txt', 'resposta.txt'])
        self.assertEqual(await form.get('files[0]').text(), prepared.text)
        self.assertEqual(form.get('files[1]').data, 'conteúdo do arquivo\n'.encode())
        self.assertEqual(payload['allowed_mentions']['parse'], [])
        self.assertLessEqual(utf16_len(payload['embeds'][0]['description']), 4096)
        self.assertIn('resposta-completa.txt', payload['content'])

    async def test_discord_file_permission_error_explains_how_to_fix(self):
        async def complete(*_args):
            return {'text': '```python\nprint("ok")\n```'}
        f = self.fixture(complete)
        async def edit(_i, text, **kwargs):
            if kwargs.get('document'):
                error = RuntimeError('private upstream details')
                error.service, error.status = 'discord', 403
                raise error
            f.outputs.append(text)
        f.b.discord.edit = edit
        await f.b.interaction(test_core.event(), await self._respond(f))
        await asyncio.gather(*list(f.b.jobs))
        self.assertIn('permissão de anexar arquivos', f.outputs[0])
        self.assertNotIn('private upstream', f.outputs[0])
        self.assertFalse(f.b.active)

    async def test_empty_source_file_is_uploaded(self):
        sent = []
        async def fetch(_url, opts):
            sent.append(opts['body'])
            return Response.json({'id': '1'})
        prepared = prepare_response('```python filename=__init__.py\n```')
        await Discord('token', fetch).output('/test', 'POST', prepared.text, document=prepared.document)
        self.assertIsInstance(sent[0], FormData)
        payload = json.loads(sent[0].get('payload_json'))
        self.assertEqual(payload['attachments'][0]['filename'], '__init__.py')
        self.assertEqual(sent[0].get('files[0]').data, b'')
