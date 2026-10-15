import json
from types import SimpleNamespace
from uuid import uuid4

from net import json_body, request, with_signal

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'generate_image',
            'description': ('Gera ou edita uma imagem com Nano Banana. Quando há foto anexada ao pedido, '
                            'ela é enviada automaticamente como imagem original para edição. '
                            'Use também para trocar fundo, retocar ou transformar o estilo da foto enviada.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'prompt': {'type': 'string', 'description': 'Descrição da imagem ou das alterações na foto original, de preferência em inglês. Preserve o que o usuário não pediu para mudar.'},
                    'caption': {'type': 'string', 'description': 'Legenda curta em português do Brasil.'},
                    'aspect_ratio': {'type': 'string', 'description': 'Omita em edições, salvo se o usuário pedir outra proporção.', 'enum': ['1:1', '16:9', '9:16', '4:3', '3:4']},
                },
                'required': ['prompt'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_video',
            'description': 'Gera um vídeo curto com Omni Flash quando o usuário pede vídeo, clipe ou animação.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'prompt': {'type': 'string', 'description': 'Descrição visual do vídeo, em inglês se possível.'},
                    'caption': {'type': 'string'},
                    'aspect_ratio': {'type': 'string', 'enum': ['16:9', '9:16']},
                    'seconds': {'type': 'integer', 'enum': [4, 6, 8]},
                },
                'required': ['prompt'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': (
                'Pesquisa na internet com o Google quando a resposta depende de informação atual ou verificável: '
                'notícias, resultados, preços, leis, clima, documentação e fatos que podem ter mudado. '
                'A data e a hora atuais já estão nas instruções; não use esta ferramenta só para saber que dia ou que horas são. '
                'Inclua o ano na consulta quando o assunto for recente. '
                'Não use para opinião, papo casual ou o que já está no histórico.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Consulta objetiva, uma pergunta ou frase de busca, no idioma do assunto.',
                    },
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_my_color',
            'description': 'Altera a cor do perfil do próprio usuário no servidor. Nunca use para mudar a cor de outra pessoa. O usuário informa a cor em hexadecimal.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'hex': {'type': 'string', 'description': 'Cor em hex, por exemplo #FF5500 ou FF5500.'},
                },
                'required': ['hex'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_server',
            'description': 'Lista categorias e canais atuais. Só para administradores, antes de reorganizar o servidor.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_category',
            'description': 'Cria uma categoria no servidor. Só administradores.',
            'parameters': {
                'type': 'object',
                'properties': {'name': {'type': 'string'}},
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_channel',
            'description': 'Cria um canal de texto, voz, anúncios ou fórum. Só administradores. Use list_server antes se precisar dos IDs das categorias.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'type': {'type': 'string', 'enum': ['text', 'voice', 'announcement', 'forum']},
                    'category': {'type': 'string', 'description': 'Nome ou ID da categoria pai.'},
                    'topic': {'type': 'string'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'edit_channel',
            'description': 'Edita um canal existente: nome, tópico, categoria, NSFW ou slowmode. Use list_server antes. channel_id é o nome/ID atual; name é o nome novo.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'channel_id': {'type': 'string', 'description': 'ID ou nome ATUAL do canal.'},
                    'channel': {'type': 'string', 'description': 'Nome atual, se não tiver o ID.'},
                    'name': {'type': 'string', 'description': 'Nome NOVO. Pode ter emoji e espaços. O | vira │ (o Discord não aceita pipe ASCII).'},
                    'topic': {'type': 'string', 'description': 'Novo tópico/descrição do canal.'},
                    'category_id': {'type': 'string'},
                    'nsfw': {'type': 'boolean'},
                    'slowmode': {'type': 'integer', 'description': 'Slowmode em segundos, 0 para tirar.'},
                },
                'required': ['channel_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_channels',
            'description': 'Cria vários canais de uma vez. Só administradores. Prefira isto a várias create_channel.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'channels': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'name': {'type': 'string'},
                                'type': {'type': 'string', 'enum': ['text', 'voice', 'announcement', 'forum']},
                                'category': {'type': 'string'},
                                'topic': {'type': 'string'},
                            },
                            'required': ['name'],
                        },
                    },
                },
                'required': ['channels'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'apply_channel_names',
            'description': 'Renomeia vários canais de uma vez para o formato emoji │ Nome. Só administradores. Prefira isto a várias edit_channel.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'renames': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'channel': {'type': 'string', 'description': 'Nome ou ID atual.'},
                                'name': {'type': 'string', 'description': 'Nome novo, ex: 🔧 │ Teste'},
                                'topic': {'type': 'string'},
                                'category': {'type': 'string'},
                            },
                            'required': ['channel', 'name'],
                        },
                    },
                },
                'required': ['renames'],
            },
        },
    },
]

IMAGE_TOOL = TOOLS[0]


class Vertex:
    def __init__(self, cfg, auth, fetcher=None):
        self.cfg = cfg
        self.auth = auth
        self.fetcher = fetcher

    async def complete(self, messages, signal=None, *, tools=None):
        selected = TOOLS if tools is None else tools
        payload = {
            'model': self.cfg.model,
            'messages': messages,
            'stream': False,
            'max_tokens': self.cfg.maxTokens,
        }
        if self.cfg.model == 'google/gemini-4-argon':
            payload['reasoning_effort'] = self.cfg.reasoningEffort
        else:
            payload['temperature'] = self.cfg.temperature
        if selected:
            payload.update(tools=selected, tool_choice='auto')
        data = await self._post(payload, signal)
        if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or not data['choices']:
            raise RuntimeError('Resposta inválida do modelo')
        choice = (data.get('choices') or [None])[0] or {}
        message = choice.get('message') or {}
        content = message.get('content')
        raw_calls = message.get('tool_calls') or []
        if not isinstance(raw_calls, list) or len(raw_calls) > self.cfg.maxToolCalls:
            raise RuntimeError('Quantidade inválida de ferramentas na resposta')
        tool_calls = []
        seen = set()
        for call in raw_calls:
            if not isinstance(call, dict) or call.get('type', 'function') != 'function' or not isinstance(call.get('function'), dict):
                raise RuntimeError('Chamada de ferramenta inválida')
            call_id = call.get('id') or f'call_{uuid4().hex}'
            if not isinstance(call_id, str) or call_id in seen:
                raise RuntimeError('Identificador de ferramenta inválido')
            seen.add(call_id)
            tool_calls.append({**call, 'id': call_id, 'type': 'function'})
        reason = choice.get('finish_reason')
        if reason in ('content_filter', 'safety'):
            raise RuntimeError('O modelo bloqueou o conteúdo.')
        if (not isinstance(content, str) or not content.strip()) and not tool_calls:
            raise RuntimeError('O modelo retornou resposta vazia')
        return SimpleNamespace(text=content if isinstance(content, str) else '',
                               tool_calls=tool_calls, truncated=reason == 'length',
                               usage=data.get('usage'),
                               extra_content=message.get('extra_content') if isinstance(message.get('extra_content'), dict) else None)

    async def _post(self, payload, signal):
        token = await with_signal(self.auth.token(), signal)
        res = await request(self.cfg.endpoint, {
            'method': 'POST',
            'timeout': self.cfg.timeout / 1000,
            'headers': {
                'authorization': f'Bearer {token}',
                'content-type': 'application/json',
                'x-goog-user-project': self.cfg.project,
            },
            'body': json.dumps(payload, separators=(',', ':'), ensure_ascii=False),
        }, signal=signal, fetcher=self.fetcher, service='vertex')
        return await json_body(res)


def validate_arguments(name, args):
    """Validate untrusted model arguments before any external side effect."""
    schema = next((t['function']['parameters'] for t in TOOLS if t['function']['name'] == name), None)
    if schema is None:
        return 'Ferramenta desconhecida.'
    return _validate(args, schema)


def _validate(value, schema, path='argumentos'):
    kind = schema.get('type')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'boolean': type(value) is bool}
    if not valid.get(kind, False):
        return f'Tipo inválido em {path}: esperado {kind}.'
    if 'enum' in schema and value not in schema['enum']:
        return f'Valor inválido em {path}.'
    if kind == 'string' and len(value) > 4000:
        return f'Texto muito longo em {path}.'
    if kind == 'object':
        props = schema.get('properties', {})
        if set(value) - set(props):
            return f'Campos não permitidos em {path}.'
        if any(key not in value for key in schema.get('required', [])):
            return f'Campos obrigatórios ausentes em {path}.'
        for key, item in value.items():
            error = _validate(item, props[key], f'{path}.{key}')
            if error:
                return error
    if kind == 'array':
        if not 1 <= len(value) <= 12:
            return f'Envie entre 1 e 12 itens em {path}.'
        for item in value:
            error = _validate(item, schema['items'], path)
            if error:
                return error
    return None
