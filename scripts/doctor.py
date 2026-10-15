"""Validate local configuration; optionally test the providers without posting messages."""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from importlib.metadata import version

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import config, load_env
from discord import Discord
from google_auth import GoogleAuth
from net import AbortSignal
from vertex import Vertex


async def diagnose(live=False):
    report = {'ok': True, 'checks': []}
    def check(name, ok, detail):
        report['checks'].append({'check': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            report['ok'] = False
    try:
        cfg = config()
    except ValueError as error:
        check('configuration', False, str(error))
        return report
    check('configuration', True, f'{cfg.model} · {cfg.region}')
    for package in ('cryptography', 'websockets', 'Pillow'):
        try:
            check(package, True, version(package))
        except Exception:
            check(package, False, 'Dependência ausente; instale requirements.txt')
    if cfg.db != ':memory:' and Path(cfg.db).is_file():
        conn = None
        try:
            conn = sqlite3.connect(Path(cfg.db).resolve().as_uri() + '?mode=ro', uri=True)
            healthy = conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            check('database', healthy, 'Verificação de integridade')
        except sqlite3.Error:
            check('database', False, 'Não foi possível ler o banco')
        finally:
            if conn:
                conn.close()
    else:
        check('database', True, 'Será criado na primeira execução')
    explicit = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if explicit:
        check('credentials_file', Path(explicit).is_file(), 'Arquivo configurado existe' if Path(explicit).is_file() else 'Arquivo configurado não encontrado')
    if live:
        try:
            user = await Discord(cfg.botToken).api('/users/@me')
            check('discord', str(user.get('id')) == cfg.appId, 'Identidade do bot verificada')
        except Exception as error:
            check('discord', False, f'{type(error).__name__}; HTTP {getattr(error, "status", "n/a")}')
        signal = AbortSignal.timeout(30000)
        try:
            cfg.maxTokens = 1500
            async with asyncio.timeout(30):
                result = await Vertex(cfg, GoogleAuth()).complete(
                    [{'role': 'user', 'content': 'Responda apenas OK.'}], signal, tools=[])
            check('model', bool(result.text.strip()), 'O modelo configurado respondeu; nenhuma mensagem foi enviada ao Discord')
        except Exception as error:
            check('model', False, f'{type(error).__name__}; HTTP {getattr(error, "status", "n/a")}')
        finally:
            signal.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Faz uma chamada curta ao modelo (pode gerar cobrança) e verifica o token Discord')
    args = parser.parse_args()
    load_env()
    result = asyncio.run(diagnose(args.live))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
