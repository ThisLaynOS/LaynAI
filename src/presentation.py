"""Turn model Markdown into downloadable source files without executing it."""

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath


FILE_INSTRUCTIONS = (
    'ENTREGA DE CÓDIGO E ARQUIVOS: o bot converte seus blocos em anexos reais. '
    'Para cada arquivo, use um bloco cercado por crases, com linguagem e filename=caminho/arquivo.ext '
    'na primeira linha, por exemplo: ```python filename=app.py. '
    'Coloque a quebra de linha e o conteúdo completo em seguida, fechando o bloco normalmente. '
    'Escreva as cercas sem indentação e fora de listas. '
    'Use caminhos relativos, nomes descritivos e um único bloco por arquivo. '
    'Nunca misture arquivos diferentes no mesmo bloco. Se o arquivo contiver cercas Markdown, '
    'use uma cerca externa com mais crases. Um arquivo vira anexo individual; vários viram projeto.zip '
    'preservando as pastas. Não invente links de download. '
    'Entregue uma solução completa e proporcional ao pedido, com imports e dependências necessárias; '
    'evite TODOs e reticências no lugar da implementação solicitada. '
    'Antes dos blocos, resuma em uma frase o que a solução faz. Fora dos blocos, '
    'explique como instalar, configurar e executar quando necessário, com comandos curtos. '
    'Comandos de terminal de até três linhas podem ficar em blocos sh sem filename para copiar. '
    'Para um script shell que deva ser salvo, sempre informe filename. '
    'Logs, saídas e exemplos de texto usam blocos text sem filename. '
    'Ao explicar código existente, diferencie exemplos de um programa completo. '
    'Ao corrigir, explique a mudança e entregue o arquivo atualizado se couber. '
    'Revise nomes, imports e instruções antes de responder; não diga que executou ou testou código, '
    'pois não há ferramenta de execução. Não solicite segredos: use variáveis de ambiente. '
    'Se o projeto não couber, entregue uma parte funcional claramente delimitada e diga o que falta, '
    'sem apresentar um projeto incompleto como pronto.'
)

_LANGUAGES = {
    'python': 'py', 'py': 'py', 'python3': 'py',
    'javascript': 'js', 'js': 'js', 'node': 'js', 'nodejs': 'js',
    'typescript': 'ts', 'ts': 'ts', 'jsx': 'jsx', 'tsx': 'tsx',
    'html': 'html', 'css': 'css', 'scss': 'scss', 'sass': 'sass',
    'json': 'json', 'jsonc': 'jsonc', 'yaml': 'yaml', 'yml': 'yml',
    'toml': 'toml', 'ini': 'ini', 'xml': 'xml', 'svg': 'svg',
    'sql': 'sql', 'graphql': 'graphql', 'proto': 'proto',
    'java': 'java', 'kotlin': 'kt', 'kt': 'kt', 'swift': 'swift',
    'c': 'c', 'cpp': 'cpp', 'c++': 'cpp', 'csharp': 'cs', 'c#': 'cs', 'cs': 'cs',
    'go': 'go', 'golang': 'go', 'rust': 'rs', 'rs': 'rs',
    'ruby': 'rb', 'rb': 'rb', 'php': 'php', 'lua': 'lua', 'luau': 'luau',
    'dart': 'dart', 'r': 'r', 'perl': 'pl', 'elixir': 'ex', 'erlang': 'erl',
    'bash': 'sh', 'sh': 'sh', 'shell': 'sh', 'zsh': 'zsh',
    'powershell': 'ps1', 'ps1': 'ps1', 'bat': 'bat', 'batch': 'bat', 'cmd': 'cmd',
    'dockerfile': '', 'docker': '', 'makefile': '', 'make': '',
    'markdown': 'md', 'md': 'md', 'csv': 'csv', 'diff': 'patch', 'patch': 'patch',
    'dotenv': 'env', 'env': 'env', 'text': 'txt', 'txt': 'txt', 'plaintext': 'txt',
}
_INLINE = {'text', 'txt', 'plaintext', 'console', 'output', 'log', 'logs', 'mermaid'}
_SHELL = {'sh', 'bash', 'shell', 'zsh', 'powershell', 'ps1', 'bat', 'batch', 'cmd'}
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n', re.MULTILINE)
_FILENAME = re.compile(r'''(?:^|\s)(?:filename|file|path)\s*=\s*(?:"([^"]+)"|'([^']+)'|(\S+))''', re.I)
_RESERVED = re.compile(r'^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', re.I)


@dataclass
class PreparedResponse:
    text: str
    document: dict | None
    history: str


def _looks_like_filename(value):
    return bool(re.fullmatch(r'[\w./\\-]+', value) and (
        '.' in value.rsplit('/', 1)[-1] or value.lower() in ('dockerfile', 'makefile', 'license')
    ))


def _metadata(info, before):
    info = info.strip()
    explicit = _FILENAME.search(info)
    name = next((part for part in explicit.groups() if part), '') if explicit else ''
    tokens = info.split()
    lang = tokens[0].lower() if tokens else ''
    if not name and tokens:
        candidate = tokens[-1].strip('"\'')
        if _looks_like_filename(candidate) and candidate.lower() not in _LANGUAGES:
            name = candidate
    heading_start = None
    # Recognize a standalone heading such as **`src/main.py`**, not prose mentioning a file.
    heading = re.search(r'(?m)^([^\r\n]+)\r?\n(?:[ \t]*\r?\n)*$', before)
    if heading:
        candidate = re.sub(r'^\s*(?:#{1,6}\s+|\d+[.)]\s+)', '', heading[1]).strip()
        candidate = re.sub(r'^(?:arquivo|file)\s*:\s*', '', candidate.strip('*'), flags=re.I)
        candidate = candidate.strip('`* :')
        if _looks_like_filename(candidate):
            if not name:
                name = candidate
            if candidate == name:
                heading_start = heading.start()
    return lang, name, heading_start


def _safe_path(value, fallback):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    parts = []
    for part in value.replace('\\', '/').split('/'):
        if part in ('', '.', '..'):
            continue
        part = re.sub(r'[^a-zA-Z0-9_.-]', '_', part).rstrip('. ')[:80]
        if part in ('', '.', '..'):
            continue
        if _RESERVED.match(part):
            part = '_' + part
        parts.append(part)
    return '/'.join(parts[-8:]) or fallback


def _unique_path(name, used):
    parts = name.split('/')
    # Files cannot also be parent directories, even on case-insensitive filesystems.
    for index in range(len(parts)):
        original = parts[index]
        number = 1
        while True:
            candidate = '/'.join(parts[:index + 1]).casefold()
            conflict = candidate in used
            if index == len(parts) - 1:
                conflict = conflict or any(item.startswith(candidate + '/') for item in used)
            if not conflict:
                break
            number += 1
            path = PurePosixPath(original)
            parts[index] = f'{path.stem}-{number}{path.suffix}'
    result = '/'.join(parts)
    used.add(result.casefold())
    return result


def _default_name(lang):
    if lang in ('docker', 'dockerfile'):
        return 'Dockerfile'
    if lang in ('make', 'makefile'):
        return 'Makefile'
    return 'codigo.' + _LANGUAGES.get(lang, 'txt')


def prepare_response(text, *, truncated=False):
    """Preserve source bytes, prose and history; only transform recognized fences."""
    text = text or ''
    parts, files, used = [], [], set()
    cursor = scan = 0
    incomplete = truncated
    while opening := _FENCE.search(text, scan):
        fence, info = opening.groups()
        closing = re.compile(r'^ {0,3}' + re.escape(fence[0]) + '{' + str(len(fence))
                             + r',}[ \t]*(?:\r?\n|$)', re.MULTILINE).search(text, opening.end())
        end = closing.end() if closing else len(text)
        body = text[opening.end():closing.start() if closing else len(text)]
        lang, name, heading = _metadata(info, text[cursor:opening.start()])
        extract = bool(name or (lang in _LANGUAGES and lang not in _INLINE))
        if not name and lang in _SHELL and len(body.splitlines()) <= 3:
            extract = False
        if extract and (body.strip() or name):
            indentation = len(opening[0]) - len(opening[0].lstrip(' '))
            if indentation:
                # Markdown removes up to N leading spaces from a fence indented N spaces.
                body = re.sub(r'(?m)^ {1,' + str(indentation) + '}', '', body)
            path = _safe_path(name, _default_name(lang)) if name else _default_name(lang)
            if not closing:
                path += '.partial'
                incomplete = True
            path = _unique_path(path, used)
            prose_end = cursor + heading if heading is not None else opening.start()
            parts.append(text[cursor:prose_end])
            parts.append(f'Arquivo: `{path}`\n')
            files.append((path, body.encode('utf-8')))
            cursor = end
        scan = end
    parts.append(text[cursor:])
    shown = ''.join(parts).strip()
    history = text.strip()
    if incomplete:
        warning = ('**Resposta incompleta:** a geração terminou antes de concluir. '
                   'Peça para continuar; arquivos `.partial` contêm apenas o trecho recebido.')
        if not files:
            warning = '**Resposta incompleta:** a geração terminou antes de concluir. Peça para continuar.'
        shown = warning + '\n\n' + shown
        history = warning + '\n\n' + history
    document = None
    if files:
        history += '\n\nArquivos entregues (na ordem dos blocos): ' + ', '.join(f'`{name}`' for name, _ in files) + '.'
        archive = len(files) > 1 or '/' in files[0][0]
        if archive:
            filename = 'projeto-incompleto.zip' if incomplete else 'projeto.zip'
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
                for path, data in files:
                    bundle.writestr(path, data)
                # Keep usage instructions available after downloading, without overwriting user files.
                guide = _unique_path('LEIA-ME-LaynAI.md', used)
                bundle.writestr(guide, shown.encode('utf-8'))
            document = {'filename': filename, 'mime': 'application/zip', 'data': buffer.getvalue()}
            shown = f'📎 Baixe `{filename}` e extraia os arquivos.\n\n' + shown
        else:
            filename, data = files[0]
            document = {'filename': filename, 'mime': 'text/plain;charset=utf-8', 'data': data}
            shown = f'📎 Baixe `{filename}` no anexo.\n\n' + shown
    return PreparedResponse(shown, document, history)
