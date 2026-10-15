"""Relógio real de cada pedido, no horário de Brasília.

O Brasil não usa horário de verão desde 2019, então UTC−3 vale o ano inteiro.
datetime.timezone dispensa tzdata, que a imagem slim não traz.
"""

import re
from datetime import datetime, timedelta, timezone

BRASILIA = timezone(timedelta(hours=-3))

_WEEKDAYS = (
    'segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira',
    'sexta-feira', 'sábado', 'domingo',
)
_MONTHS = (
    'janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
    'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro',
)
_RELATIVE_TIME = re.compile(
    r'\b(hoje|ontem|amanh[ãa]|agora|'
    r'este ano|esse ano|neste ano|'
    r'este m[eê]s|esse m[eê]s|neste m[eê]s|'
    r'esta semana|essa semana|nesta semana|semana passada)\b',
    re.IGNORECASE,
)
_YEAR = re.compile(r'\b20\d{2}\b')


def moment(now=None):
    if now is None:
        return datetime.now(BRASILIA)
    if now.tzinfo is None:
        return now.replace(tzinfo=BRASILIA)
    return now.astimezone(BRASILIA)


def clock_line(now=None):
    current = moment(now)
    weekday = _WEEKDAYS[current.weekday()]
    month = _MONTHS[current.month - 1]
    return (
        f'{weekday}, {current.day} de {month} de {current.year}, '
        f'{current.hour:02d}:{current.minute:02d} (horário de Brasília)'
    )


def clock_note(now=None):
    current = moment(now)
    month = _MONTHS[current.month - 1]
    return (
        'DATA E HORA ATUAIS: '
        f'{clock_line(current)}. '
        f'Ano {current.year}, mês {current.month} ({month}), dia {current.day}, '
        f'hora {current.hour}, minuto {current.minute}. '
        'Estes valores são o momento real deste pedido, lidos do relógio do servidor. '
        'Use-os quando perguntarem que dia é hoje, que horas são, o ano ou o mês. '
        'Não diga que não tem acesso à data e não invente outro calendário. '
        'Não chame web_search só para descobrir a data ou a hora. '
        'Quando a resposta depender de notícia, resultado, preço, lei, clima, documentação '
        'ou qualquer fato que possa ter mudado, chame web_search antes de responder '
        'e inclua este ano na consulta se o assunto for recente.'
    )


def ground_query(query, now=None):
    """Ancora 'hoje', 'ontem' e 'este ano' na data real antes de ir ao Google."""
    text = (query or '').strip()
    if not text or not _RELATIVE_TIME.search(text) or _YEAR.search(text):
        return text
    current = moment(now)
    month = _MONTHS[current.month - 1]
    return f'{text} — hoje é {current.day} de {month} de {current.year}'
