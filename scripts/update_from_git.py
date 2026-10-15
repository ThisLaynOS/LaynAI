"""Install approved origin/main commits for the local OpenRC service.

Run as the user who manages ``rc-service laynai`` (root on this host).  A cron
entry may call this repeatedly; only a new, validated commit restarts the bot.
"""

import argparse
import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / 'data'
DEPLOYED_REVISION = STATE_DIR / '.deployed-revision'
FAILED_REVISION = STATE_DIR / '.failed-revision'
READY_URL = 'http://127.0.0.1:8080/readyz'
SERVICE = 'laynai'
BACKUP_DIR = ROOT / 'backups' / 'restorepoints'


def command(*args, cwd=ROOT, capture=False, new_session=False):
    result = subprocess.run(args, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            start_new_session=new_session)
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


def start_service():
    command('rc-service', SERVICE, 'start', new_session=True)
    if not ready():
        raise RuntimeError(f'{SERVICE} não ficou pronto após a inicialização.')
    command('rc-service', SERVICE, 'status')


def stop_service():
    command('rc-service', SERVICE, 'stop', new_session=True)


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


def database_path():
    sys.path.insert(0, str(ROOT / 'src'))
    from config import load_env
    load_env(str(ROOT / '.env'))
    return (ROOT / os.environ.get('DATABASE_PATH', './data/bot.sqlite')).resolve()


def backup_database(source, destination):
    command(sys.executable, 'scripts/backup.py', '--database', str(source),
            '--output', str(destination))


def backup_state(revision):
    """Save everything this updater can change before installing new code."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUP_DIR, 0o700)
    staging = Path(tempfile.mkdtemp(prefix='.pending-', dir=BACKUP_DIR))
    destination = BACKUP_DIR / f'{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{revision[:12]}'
    try:
        db_path = database_path()
        git('archive', '--format=tar', f'--output={staging / "code.tar"}', revision)
        os.chmod(staging / 'code.tar', 0o600)
        shutil.copy2(ROOT / '.env', staging / '.env')
        os.chmod(staging / '.env', 0o600)
        backup_database(db_path, staging / 'bot.sqlite')
        (staging / 'revision').write_text(revision + '\n')
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging)
        raise
    print(f'Ponto de restauração: {destination}', flush=True)
    return destination, db_path


def restore_database(snapshot, destination):
    source = snapshot / 'bot.sqlite'
    with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup SQLite inválido; restauração interrompida.')
    fd, temporary = tempfile.mkstemp(prefix='.restore-', dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o600)
        for suffix in ('-wal', '-shm'):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore_previous(branch, previous, snapshot, db_path):
    stop_service()
    if git('status', '--porcelain', '--untracked-files=no', capture=True):
        raise RuntimeError('Arquivos versionados mudaram durante a falha; restaure manualmente.')
    git('switch', '--detach', previous)
    git('branch', '--force', branch, previous)
    git('switch', branch)
    shutil.copy2(snapshot / '.env', ROOT / '.env')
    os.chmod(ROOT / '.env', 0o600)
    restore_database(snapshot, db_path)
    start_service()


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
    stop_service()
    try:
        snapshot, db_path = backup_state(current)
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        start_service()
        raise
    try:
        git('merge', '--ff-only', target)
        start_service()
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        write_revision(FAILED_REVISION, target)
        print(f'Restaurando {current[:12]} de {snapshot}...', file=sys.stderr, flush=True)
        try:
            restore_previous(branch, current, snapshot, db_path)
        except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
            raise RuntimeError(f'Rollback automático falhou; ponto de restauração preservado em {snapshot}.') from error
        raise RuntimeError(f'A revisão {target[:12]} falhou; código e banco restaurados em {current[:12]}.')

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
