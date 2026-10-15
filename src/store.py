import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from clock import clock_note


def _day(now_ms):
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')


class Store:
    def __init__(self, path):
        if path != ':memory:':
            os.makedirs(os.path.dirname(path) or '.', mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.isolation_level = None
        self.db.executescript(
            '''PRAGMA journal_mode=WAL; PRAGMA busy_timeout=3000; PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, messages TEXT NOT NULL, updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, subject TEXT NOT NULL, count INTEGER NOT NULL, last INTEGER NOT NULL, PRIMARY KEY(day,subject));
            CREATE TABLE IF NOT EXISTS interactions (id TEXT PRIMARY KEY, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS token_usage (
                day TEXT NOT NULL, subject TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day, subject));
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY, answer TEXT NOT NULL, sources TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated);
            CREATE INDEX IF NOT EXISTS interactions_created ON interactions(created);
            CREATE INDEX IF NOT EXISTS pages_created ON pages(created);'''
        )
        if path != ':memory:':
            os.chmod(path, 0o600)

    def claim(self, id, now=None):
        now = _now(now)
        cur = self.db.execute('INSERT OR IGNORE INTO interactions VALUES (?,?)', (id, now))
        return cur.rowcount > 0

    def history(self, key, ttl, now=None):
        now = _now(now)
        row = self.db.execute('SELECT messages,updated FROM sessions WHERE key=?', (key,)).fetchone()
        if not row or now - row['updated'] >= ttl:
            return []
        try:
            messages = json.loads(row['messages'])
        except (ValueError, TypeError):
            return []
        return _valid_turns(messages)

    def save(self, key, messages, now=None):
        now = _now(now)
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?)', (key, json.dumps(messages, separators=(',', ':'), ensure_ascii=False), now))

    def clear(self, key):
        self.db.execute('DELETE FROM sessions WHERE key=?', (key,))

    def consume(self, user, cfg, now=None):
        now = _now(now)
        day = _day(now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            def get(s):
                return self.db.execute('SELECT count,last FROM usage WHERE day=? AND subject=?', (day, s)).fetchone()
            u, g = get(user), get('*')
            reason = None
            if u and now - u['last'] < cfg.cooldown * 1000:
                reason = 'Aguarde alguns segundos antes de perguntar novamente.'
            elif u and u['count'] >= cfg.userDaily:
                reason = 'Você atingiu o limite diário. Ele renova à meia-noite UTC.'
            elif g and g['count'] >= cfg.globalDaily:
                reason = 'O bot atingiu o limite global diário. Tente amanhã (UTC).'
            if not reason:
                for subject in (user, '*'):
                    self.db.execute(
                        '''INSERT INTO usage VALUES (?,?,1,?)
                        ON CONFLICT(day,subject) DO UPDATE SET count=count+1,last=excluded.last''',
                        (day, subject, now),
                    )
            self.db.execute('COMMIT')
            return reason
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def count(self, user, now=None):
        now = _now(now)
        row = self.db.execute('SELECT count FROM usage WHERE day=? AND subject=?', (_day(now), user)).fetchone()
        return row['count'] if row else 0

    def record_tokens(self, user, usage, now=None):
        day = _day(_now(now))
        values = [usage.get(k, 0) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        values = [v if type(v) is int and 0 <= v < 2**40 else 0 for v in values]
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for subject in (user, '*'):
                self.db.execute('''INSERT INTO token_usage VALUES (?,?,?,?,?,1)
                    ON CONFLICT(day,subject) DO UPDATE SET
                    prompt_tokens=prompt_tokens+excluded.prompt_tokens,
                    completion_tokens=completion_tokens+excluded.completion_tokens,
                    total_tokens=total_tokens+excluded.total_tokens, calls=calls+1''', (day, subject, *values))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def tokens(self, user, now=None):
        row = self.db.execute('SELECT prompt_tokens,completion_tokens,total_tokens,calls FROM token_usage WHERE day=? AND subject=?',
                              (_day(_now(now)), user)).fetchone()
        return dict(row) if row else dict(prompt_tokens=0, completion_tokens=0, total_tokens=0, calls=0)

    def save_pages(self, answer, sources, now=None):
        page_id = secrets.token_hex(8)
        self.db.execute(
            'INSERT INTO pages (id, answer, sources, created) VALUES (?,?,?,?)',
            (page_id, answer or '', sources or '', _now(now)),
        )
        return page_id

    def pages(self, page_id):
        if not isinstance(page_id, str):
            return None
        row = self.db.execute('SELECT answer, sources FROM pages WHERE id=?', (page_id,)).fetchone()
        if not row:
            return None
        return {'answer': row['answer'], 'sources': row['sources']}

    def healthy(self):
        return self.db.execute('SELECT 1').fetchone()[0] == 1

    def cleanup(self, ttl, now=None):
        now = _now(now)
        self.db.execute('DELETE FROM sessions WHERE updated<?', (now - ttl,))
        self.db.execute('DELETE FROM interactions WHERE created<?', (now - 900000,))
        self.db.execute('DELETE FROM usage WHERE day<?', (_day(now - 2 * 86400000),))
        self.db.execute('DELETE FROM token_usage WHERE day<?', (_day(now - 30 * 86400000),))
        self.db.execute('DELETE FROM pages WHERE created<?', (now - 7 * 86400000,))

    def backup(self, destination):
        """SQLite online backup API produces a consistent snapshot, including WAL."""
        if os.path.exists(destination):
            raise ValueError('O destino do backup já existe')
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
        finally:
            target.close()

    def close(self):
        self.db.close()


def context(history, prompt, cfg, now=None):
    # Drop whole user/assistant turns, preserving role alternation.
    # The clock is rebuilt on every request and is not stored in history.
    system = f'{cfg.system}\n\n{clock_note(now)}'
    pairs = _valid_turns(history)[-cfg.historyPairs * 2:]
    while pairs and len(json.dumps(pairs, separators=(',', ':'), ensure_ascii=False)) + len(prompt) + len(system) > cfg.contextChars:
        del pairs[:2]
    return [{'role': 'system', 'content': system}, *pairs, {'role': 'user', 'content': prompt}]


def _valid_turns(messages):
    if not isinstance(messages, list):
        return []
    # Historical system/tool roles must never be replayed as trusted instructions.
    return [{'role': m['role'], 'content': m['content']} for m in messages
            if isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)]


def _now(now):
    if now is None:
        from time import time
        return int(time() * 1000)
    return now
