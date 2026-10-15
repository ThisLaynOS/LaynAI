"""Create a consistent SQLite snapshot without stopping the bot."""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import load_env


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('Banco de origem não encontrado')
    if source == destination:
        raise ValueError('O backup precisa de um caminho diferente do banco')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = dst = None
    try:
        src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)
        dst = sqlite3.connect(destination)
        src.backup(dst, pages=256)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Falha na verificação do backup')
    finally:
        if dst:
            dst.close()
        if src:
            src.close()
    return destination


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=os.environ.get('DATABASE_PATH', './data/bot.sqlite'))
    parser.add_argument('--output', default=f"./backups/bot-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite")
    args = parser.parse_args()
    try:
        result = backup(args.database, args.output)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        print(f'Backup não concluído: {type(error).__name__}. Verifique caminhos, permissões e integridade.', file=sys.stderr)
        return 1
    print(f'Backup consistente criado: {result}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
