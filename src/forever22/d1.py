"""Small-dataset transactional D1 client; no SQL is exposed over HTTP."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from importlib.resources import files
from urllib.parse import urlparse

import httpx

from .config import _env_value

MAX_SNAPSHOT = 16 * 1024 * 1024
MAX_COMMIT = 8 * 1024 * 1024


class D1Error(RuntimeError):
    pass


class D1Conflict(D1Error):
    pass


def enabled() -> bool:
    return bool(_env_value('FOREVER22_D1_URL'))


def _sql(sql: str) -> str:
    # Only translate tokens outside quoted strings/identifiers.
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'\bnow\(\)', 'CURRENT_TIMESTAMP', parts[i], flags=re.I).replace('%s', '?')
    return ''.join(parts)


class D1Connection:
    def __init__(self, *, rpc=None):
        self._http = None
        self._failed = False
        self._conn = None
        if rpc is None:
            url = (_env_value('FOREVER22_D1_URL') or '').rstrip('/')
            token = _env_value('FOREVER22_D1_TOKEN')
            parsed = urlparse(url)
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')):
                raise D1Error('FOREVER22_D1_URL must use HTTPS (HTTP allowed only for local tests).')
            if not token:
                raise D1Error('FOREVER22_D1_TOKEN is not set.')
            self._http = httpx.Client(base_url=url, headers={'Authorization': f'Bearer {token}'}, timeout=60)
            rpc = self._request
        self._rpc = rpc
        try:
            snapshot = self._rpc('/snapshot')
            self._version = snapshot['version']
            self._conn = sqlite3.connect(':memory:')
            self._conn.row_factory = lambda cur, row: dict(zip((d[0] for d in cur.description), row))
            self._conn.execute('PRAGMA foreign_keys = ON')
            self._conn.executescript(files('forever22').joinpath('d1_schema.sql').read_text())
            self._tables = {}
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                table = row['name']
                info = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                self._tables[table] = ([r['name'] for r in info], [r['name'] for r in sorted(info, key=lambda r: r['pk']) if r['pk']])
            for table, rows in snapshot['tables'].items():
                columns, _ = self._tables[table]
                for row in rows:
                    self._conn.execute(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', [row[c] for c in columns])
            self._conn.commit()
            self._baseline = self._rows()
        except Exception:
            self.close()
            raise

    def _request(self, path, body=None):
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        if data and len(data) > MAX_COMMIT:
            raise D1Error('D1 transaction exceeds the 8 MiB client limit; no changes sent.')
        try:
            with self._http.stream('GET' if body is None else 'POST', path, content=data,
                                   headers={'Content-Type': 'application/json'}) as response:
                if response.status_code == 409:
                    raise D1Conflict('Database changed during this operation. Changes were not saved; refresh before trying again.')
                if response.status_code != 200:
                    raise D1Error(f'D1 service returned HTTP {response.status_code}; check service configuration.')
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_SNAPSHOT:
                        raise D1Error('D1 snapshot exceeds the 16 MiB client limit.')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
        except httpx.HTTPError:
            raise D1Error('D1 connection failed. A submitted commit may have succeeded; inspect state before retrying.') from None

    def _rows(self):
        return {table: {tuple(row[k] for k in keys): row for row in self._conn.execute(f'SELECT * FROM "{table}"')}
                for table, (_, keys) in self._tables.items()}

    def execute(self, sql, params=()):
        if self._failed:
            raise D1Error('Connection cannot be reused after a failed commit.')
        return self._conn.execute(_sql(sql), params)

    def commit(self):
        if self._failed:
            raise D1Error('Connection cannot be reused after a failed commit.')
        current = self._rows()
        changes = {}
        for table, rows in current.items():
            before = self._baseline[table]
            upserts = [row for key, row in rows.items() if before.get(key) != row]
            deletes = [dict(zip(self._tables[table][1], key)) for key in before.keys() - rows.keys()]
            if upserts or deletes:
                changes[table] = {'upserts': upserts, 'deletes': deletes}
        if changes:
            try:
                result = self._rpc('/commit', {'version': self._version, 'request_id': str(uuid.uuid4()), 'changes': changes})
                self._version = result['version']
            except Exception:
                self._failed = True
                raise
        self._conn.commit()
        self._baseline = current

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._http is not None:
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
        finally:
            self.close()
