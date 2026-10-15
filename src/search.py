import json

from clock import clock_line, ground_query
from net import json_body, request

MAX_ANSWER = 1800
MAX_SOURCES = 6


def search_instruction(now=None):
    return (
        'Use o Google Search antes de responder. '
        f'O momento atual é {clock_line(now)}. '
        'Se a consulta disser hoje, ontem, agora, este mês ou este ano, use essa data. '
        'Resuma em português do Brasil, de forma curta e factual, só com o que as fontes confirmarem.'
    )


class WebSearch:
    """Grounding with Google Search on Vertex, called as a tool by the text model."""

    def __init__(self, cfg, auth, fetcher=None):
        self.cfg = cfg
        self.auth = auth
        self.fetcher = fetcher

    async def search(self, query, signal=None, now=None):
        token = await self.auth.token()
        res = await request(self.cfg.searchEndpoint, {
            'method': 'POST',
            'timeout': self.cfg.timeout / 1000,
            'headers': {
                'authorization': f'Bearer {token}',
                'content-type': 'application/json',
                'x-goog-user-project': self.cfg.project,
            },
            'body': json.dumps({
                'systemInstruction': {'parts': [{'text': search_instruction(now)}]},
                'contents': [{'role': 'user', 'parts': [{'text': ground_query(query, now)}]}],
                'tools': [{'googleSearch': {}}],
                'generationConfig': {'temperature': 1.0, 'maxOutputTokens': 800},
            }, separators=(',', ':'), ensure_ascii=False),
        }, signal=signal, fetcher=self.fetcher, service='search')
        data = await json_body(res)
        return parse_search(query, data)


def parse_search(query, data):
    if not isinstance(data, dict):
        raise RuntimeError('A pesquisa não retornou fontes')
    feedback = data.get('promptFeedback') or data.get('prompt_feedback') or {}
    if isinstance(feedback, dict) and feedback.get('blockReason') or feedback.get('block_reason'):
        raise RuntimeError('A pesquisa bloqueou o conteúdo')
    candidates = data.get('candidates')
    candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    reason = str(candidate.get('finishReason') or candidate.get('finish_reason') or '')
    if reason.upper() in ('SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'RECITATION'):
        raise RuntimeError('A pesquisa bloqueou o conteúdo')
    parts = ((candidate.get('content') or {}).get('parts')) or []
    texts = [
        part['text'].strip() for part in parts
        if isinstance(part, dict) and isinstance(part.get('text'), str) and part['text'].strip()
    ]
    answer = '\n'.join(texts).strip()
    if len(answer) > MAX_ANSWER:
        answer = answer[:MAX_ANSWER].rstrip() + '…'
    meta = candidate.get('groundingMetadata') or candidate.get('grounding_metadata') or {}
    if not isinstance(meta, dict):
        meta = {}
    sources, seen = [], set()
    chunks = meta.get('groundingChunks') or meta.get('grounding_chunks') or []
    if isinstance(chunks, list):
        for chunk in chunks:
            web = chunk.get('web') if isinstance(chunk, dict) else None
            if not isinstance(web, dict):
                continue
            url = web.get('uri') or web.get('url') or ''
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')) or url in seen:
                continue
            seen.add(url)
            title = web.get('title') if isinstance(web.get('title'), str) else ''
            title = title.strip() or (web.get('domain') if isinstance(web.get('domain'), str) else '') or url
            sources.append({'title': title[:200], 'url': url})
            if len(sources) >= MAX_SOURCES:
                break
    searches = []
    raw_queries = meta.get('webSearchQueries') or meta.get('web_search_queries') or []
    if isinstance(raw_queries, list):
        for item in raw_queries:
            if isinstance(item, str) and item.strip() and item.strip() not in searches:
                searches.append(item.strip()[:300])
            if len(searches) >= 3:
                break
    if not sources:
        raise RuntimeError('A pesquisa não encontrou fontes')
    return {'query': query, 'answer': answer, 'sources': sources, 'searches': searches or [query]}
