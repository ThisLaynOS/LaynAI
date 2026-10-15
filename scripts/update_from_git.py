"""Install approved changes from origin/main on a Linux Docker Compose host.

Run this script on the host, outside the bot container. A timer may call it
repeatedly; it only deploys a new commit after the local checks pass.
"""

import argparse
import fcntl
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / 'data'
DEPLOYED_REVISION = STATE_DIR / '.deployed-revision'
IMAGE = 'laynai-bot:local'
ROLLBACK_IMAGE = 'laynai-bot:rollback'
READY_URL = 'http://127.0.0.1:8080/readyz'
COMPOSE_FILES = ()


def command(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def git(*args, capture=False):
    return command('git', *args, capture=capture)


def compose(*args, capture=False):
    return command('docker', 'compose', *COMPOSE_FILES, *args, capture=capture)


def ready(timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(READY_URL, timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    return False


def deploy(branch):
    if git('symbolic-ref', '--quiet', '--short', 'HEAD', capture=True) != branch:
        raise RuntimeError(f'O checkout de implantação precisa estar na branch {branch}.')
    if git('status', '--porcelain', '--untracked-files=no', capture=True):
        raise RuntimeError('Há alterações locais em arquivos versionados; implantação cancelada.')
    if not (ROOT / '.env').is_file():
        raise RuntimeError('Crie o .env local antes de implantar.')

    git('fetch', '--quiet', 'origin', branch)
    current = git('rev-parse', 'HEAD', capture=True)
    target = git('rev-parse', 'FETCH_HEAD', capture=True)
    if current != target:
        ancestor = subprocess.run(('git', 'merge-base', '--is-ancestor', current, target), cwd=ROOT)
        if ancestor.returncode != 0:
            raise RuntimeError('O histórico remoto divergiu; verifique antes de implantar.')
        git('merge', '--ff-only', target)
    if DEPLOYED_REVISION.is_file() and DEPLOYED_REVISION.read_text().strip() == target:
        print(f'Sem atualização: {target[:12]}')
        return

    print(f'Validando {target[:12]}', flush=True)
    command(sys.executable, '-m', 'unittest', 'discover', '-s', 'test', '-q')
    compose('config', '--quiet')

    old_container = compose('ps', '-q', 'bot', capture=True)
    if old_container:
        old_image = command('docker', 'inspect', '--format={{.Image}}', old_container, capture=True)
        command('docker', 'image', 'tag', old_image, ROLLBACK_IMAGE)

    compose('build', 'bot')
    try:
        compose('up', '-d', '--no-build', 'bot')
        if not ready():
            raise RuntimeError('O bot não ficou pronto após a atualização.')
    except (subprocess.CalledProcessError, RuntimeError):
        if old_container:
            print('Restaurando a imagem anterior...', file=sys.stderr, flush=True)
            command('docker', 'image', 'tag', ROLLBACK_IMAGE, IMAGE)
            compose('up', '-d', '--no-build', '--force-recreate', 'bot')
            if not ready():
                raise RuntimeError('A restauração também falhou; verifique os logs do bot.')
        raise

    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    temp = DEPLOYED_REVISION.with_suffix('.tmp')
    temp.write_text(target + '\n')
    os.replace(temp, DEPLOYED_REVISION)
    print(f'Implantado: {target[:12]}', flush=True)


def main():
    global COMPOSE_FILES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--branch', default='main', help='Branch aprovada (padrão: main)')
    parser.add_argument('--with-google-credentials-file', action='store_true',
                        help='Usa também compose.credentials.yaml')
    args = parser.parse_args()
    if args.with_google_credentials_file:
        COMPOSE_FILES = ('-f', 'compose.yaml', '-f', 'compose.credentials.yaml')
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    with (STATE_DIR / '.update.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Outra atualização já está em andamento.')
            return 0
        try:
            deploy(args.branch)
        except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
            print(f'Atualização não concluída: {error}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
