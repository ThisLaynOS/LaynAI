import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from config import load_env
from discord import Discord, commands


async def run():
    app = os.environ.get('DISCORD_APPLICATION_ID')
    token = os.environ.get('DISCORD_BOT_TOKEN')
    guild = os.environ.get('DISCORD_GUILD_ID') or ''
    if guild == (app or ''):
        # O ID da aplicação não é um servidor. Registro de guild falha com 403 e
        # comando de guild não aparece em DM.
        guild = ''
    if not token or not re.fullmatch(r'\d{17,20}', app or '') or (guild and not re.fullmatch(r'\d{17,20}', guild)):
        print('Preencha DISCORD_BOT_TOKEN, DISCORD_APPLICATION_ID e opcionalmente DISCORD_GUILD_ID.', file=sys.stderr)
        sys.exit(1)
    api = Discord(token)
    try:
        for command in commands:
            global_cmd = not guild
            path = f"/applications/{app}{'' if global_cmd else f'/guilds/{guild}'}/commands"
            payload = dict(command)
            if guild:
                for field in ('contexts', 'integration_types', 'dm_permission'):
                    payload.pop(field, None)
            await api.api(path, 'POST', payload)
            print(f"Registrado: /{command['name']} ({'global' if global_cmd else 'guild'})")
    except Exception as e:
        status = getattr(e, 'status', None) or 'conexão'
        print(f'Falha no registro: {status}. Verifique token e IDs.', file=sys.stderr)
        sys.exit(1)


def main():
    root = Path(__file__).resolve().parents[1]
    load_env(root / '.env')
    load_env('.env')
    asyncio.run(run())


if __name__ == '__main__':
    main()
