"""Install approved origin/main commits for the local OpenRC service.

Run as the user who manages ``rc-service laynai`` (root on this host).  A cron
entry may call this repeatedly; only a new, validated commit restarts the bot.
"""

import argparse
import fcntl
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / 'data'
DEPLOYED_REVISION = STATE_DIR / '.deployed-revision'
FAILED_REVISION = STATE_DIR / '.failed-revision'
READY_URL = 'http://127.0.0.1:8080/readyz'
SERVICE = 'laynai'


def command(*args, cwd=ROOT, capture=False):
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def git(*args, capture=False):
    return command('git', *args, capture=capture)


def write_revision(path, revision):
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(revision + '\n')
    os.replace(temporary, path)


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


def restart():
    command('rc-service', SERVICE, 'restart')
    if not ready():
        raise RuntimeError(f'{SERVICE} não ficou pronto após o reinício.')


def validate(revision):
    with tempfile.TemporaryDirectory(prefix='laynai-update-') as temporary:
        candidate = Path(temporary) / 'source'
        git('worktree', 'add', '--quiet', '--detach', str(candidate), revision)
        try:
            command(sys.executable, '-m', 'compileall', '-q', 'src', 'scripts', cwd=candidate)
            command(sys.executable, '-m', 'unittest', 'discover', '-s', 'test', '-q', cwd=candidate)
        finally:
            git('worktree', 'remove', '--force', str(candidate))


def ensure_clean_branch(branch):
    if git('symbolic-ref', '--quiet', '--short', 'HEAD', capture=True) != branch:
        raise RuntimeError(f'O checkout de implantação precisa estar na branch {branch}.')
    if git('status', '--porcelain', '--untracked-files=no', capture=True):
        raise RuntimeError('Há alterações locais em arquivos versionados; implantação cancelada.')
    if not (ROOT / '.env').is_file():
        raise RuntimeError('O .env local não foi encontrado.')


def ensure_fast_forward(current, target):
    result = subprocess.run(('git', 'merge-base', '--is-ancestor', current, target), cwd=ROOT)
    if result.returncode == 1:
        raise RuntimeError('O histórico remoto divergiu; verifique antes de implantar.')
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)


def ensure_same_dependencies(current, target):
    result = subprocess.run(('git', 'diff', '--quiet', current, target, '--',
                             'requirements.lock'), cwd=ROOT)
    if result.returncode == 1:
        raise RuntimeError('requirements.lock mudou; atualize o .venv manualmente antes de implantar.')
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)


def backup_database(revision):
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = ROOT / 'backups' / f'before-{revision[:12]}-{timestamp}.sqlite'
    command(sys.executable, 'scripts/backup.py', '--output', str(destination))


def restore_previous(branch, previous):
    if git('status', '--porcelain', '--untracked-files=no', capture=True):
        raise RuntimeError('Arquivos versionados mudaram durante a falha; restaure manualmente.')
    git('switch', '--detach', previous)
    git('branch', '--force', branch, previous)
    git('switch', branch)
    restart()


def deploy(branch='main'):
    ensure_clean_branch(branch)
    git('fetch', '--quiet', 'origin', branch)
    current = git('rev-parse', 'HEAD', capture=True)
    target = git('rev-parse', 'FETCH_HEAD', capture=True)

    if current == target:
        if not ready(timeout=3):
            raise RuntimeError('O bot atual não está pronto; verifique o serviço.')
        if not DEPLOYED_REVISION.is_file() or DEPLOYED_REVISION.read_text().strip() != current:
            write_revision(DEPLOYED_REVISION, current)
        print(f'Sem atualização: {current[:12]}', flush=True)
        return

    ensure_fast_forward(current, target)
    if FAILED_REVISION.is_file() and FAILED_REVISION.read_text().strip() == target:
        raise RuntimeError(f'A revisão {target[:12]} falhou antes; aguardando novo commit.')
    ensure_same_dependencies(current, target)
    print(f'Validando {target[:12]}', flush=True)
    validate(target)
    backup_database(current)
    git('merge', '--ff-only', target)

    try:
        restart()
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        write_revision(FAILED_REVISION, target)
        print(f'Restaurando {current[:12]}...', file=sys.stderr, flush=True)
        restore_previous(branch, current)
        raise RuntimeError(f'A revisão {target[:12]} falhou; serviço restaurado em {current[:12]}.')

    write_revision(DEPLOYED_REVISION, target)
    FAILED_REVISION.unlink(missing_ok=True)
    print(f'Implantado: {target[:12]}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--branch', default='main', help='Branch aprovada (padrão: main)')
    args = parser.parse_args()
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
