"""Local snapshot history and verified recovery, using the backup toolkit."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import uuid
import archive_tools


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TimeMachine:
    def __init__(self, data, source=None):
        self.data = Path(data).resolve()
        self.source = Path(source).resolve() if source else self.data / 'source'
        if self.source == self.data or self.source in self.data.parents:
            raise ValueError('Data directory must be outside the source tree')
        for reserved in (self.data / 'backups', self.data / 'recovery'):
            if self.source == reserved or reserved in self.source.parents:
                raise ValueError('Source cannot be inside backup/recovery storage')
        self.demo_allowed = source is None
        self.lock = threading.RLock()
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source and not self.source.is_dir():
            raise ValueError('Configured source directory must already exist')
        self.source.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backups = self.data / 'backups'
        self.recovery = self.data / 'recovery'
        for folder in (self.backups, self.recovery):
            folder.mkdir(exist_ok=True, mode=0o700)
        self.database = self.data / 'history.sqlite3'
        with self.db() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TEXT NOT NULL,
                archive TEXT NOT NULL, file_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL, manifest TEXT NOT NULL,
                verification_status TEXT NOT NULL, last_verified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS recoveries (
                id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
                created_at TEXT NOT NULL, folder TEXT NOT NULL,
                file_count INTEGER NOT NULL, files TEXT NOT NULL
            );
            ''')

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                yield conn
        finally:
            conn.close()

    def snapshots(self):
        with self.db() as conn:
            return [dict(row) for row in conn.execute('''SELECT id,label,created_at,file_count,
                total_bytes,verification_status,last_verified_at FROM snapshots
                ORDER BY created_at DESC,rowid DESC''')]

    def recoveries(self):
        with self.db() as conn:
            rows = conn.execute('SELECT id,snapshot_id,created_at,folder,file_count FROM recoveries ORDER BY rowid DESC LIMIT 30')
            return [dict(row) for row in rows]

    def recovery_count(self):
        with self.db() as conn:
            return conn.execute('SELECT count(*) FROM recoveries').fetchone()[0]

    def detail(self, snapshot_id):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError('Snapshot not found')
        item = dict(row)
        item['manifest'] = json.loads(item['manifest'])
        return item

    def files(self, snapshot_id):
        manifest = self.detail(snapshot_id)['manifest']
        entries = sorted(manifest['files'].items())
        return {'total': len(entries), 'limit': 1000, 'items': [
            {'path': name, 'size': item['size'], 'sha256': item['sha256']}
            for name, item in entries[:1000]]}

    def create(self, label='Manual snapshot'):
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError('Label must contain 1–80 characters')
        with self.lock:
            path = archive_tools.create(self.source, self.backups)
            try:
                manifest = archive_tools.verify(path)
                snapshot_id = uuid.uuid4().hex
                timestamp = now()
                with self.db() as conn:
                    conn.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)',
                        (snapshot_id, label.strip(), timestamp, path.name, len(manifest['files']),
                         sum(item['size'] for item in manifest['files'].values()),
                         json.dumps(manifest), 'verified', timestamp))
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return {'id': snapshot_id, 'file_count': len(manifest['files'])}

    def verify(self, snapshot_id):
        with self.lock:
            item = self.detail(snapshot_id)
            try:
                manifest = archive_tools.verify(self.backups / item['archive'])
                if manifest != item['manifest']:
                    raise ValueError('Archive manifest differs from saved snapshot metadata')
            except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
                with self.db() as conn:
                    conn.execute("UPDATE snapshots SET verification_status='failed' WHERE id=?", (snapshot_id,))
                raise ValueError('Snapshot verification failed; archive is missing, damaged, or inconsistent') from None
            timestamp = now()
            with self.db() as conn:
                conn.execute("UPDATE snapshots SET verification_status='verified',last_verified_at=? WHERE id=?", (timestamp,snapshot_id))
            return {'verified': True, 'checked_at': timestamp, 'file_count': len(manifest['files'])}

    def compare(self, before_id, after_id):
        before = self.detail(before_id)['manifest']['files']
        after = self.detail(after_id)['manifest']['files']
        changes = []
        counts = {'added': 0, 'missing': 0, 'changed': 0, 'unchanged': 0}
        for name in sorted(before.keys() | after.keys()):
            if name not in before:
                kind = 'added'
            elif name not in after:
                kind = 'missing'
            elif before[name]['sha256'] != after[name]['sha256']:
                kind = 'changed'
            else:
                counts['unchanged'] += 1
                continue
            counts[kind] += 1
            if len(changes) < 1000:
                changes.append({'path': name, 'kind': kind})
        return {'counts': counts, 'changes': changes, 'limit': 1000,
                'note': 'Compared recorded content manifests, not the current source folder'}

    def restore(self, snapshot_id, files):
        if not isinstance(files, list) or not 1 <= len(files) <= 1000 or any(not isinstance(name,str) for name in files):
            raise ValueError('Select 1–1000 file paths')
        if len(set(files)) != len(files):
            raise ValueError('Duplicate file selections are not allowed')
        with self.lock:
            item = self.detail(snapshot_id)
            for name in files:
                archive_tools.safe_name(name)
                if name not in item['manifest']['files']:
                    raise ValueError('Selected file is not in this snapshot')
            self.verify(snapshot_id)
            stage = Path(tempfile.mkdtemp(prefix='.restore-', dir=self.recovery))
            final = self.recovery / ('restore-' + uuid.uuid4().hex[:12])
            try:
                with tarfile.open(self.backups / item['archive'], 'r:gz') as archive:
                    manifest, members = archive_tools.inspect_archive(archive, 10 * 1024**3)
                    if manifest != item['manifest']:
                        raise ValueError('Snapshot metadata changed')
                    for name in files:
                        target = stage / name
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with archive.extractfile(members['data/' + name]) as source, target.open('xb') as output:
                            shutil.copyfileobj(source, output)
                        target.chmod(0o600)
                        with target.open('rb') as stream:
                            if archive_tools.digest(stream) != manifest['files'][name]['sha256']:
                                raise ValueError('Restored file checksum mismatch')
                stage.rename(final)
                timestamp = now()
                try:
                    with self.db() as conn:
                        conn.execute('INSERT INTO recoveries VALUES (?,?,?,?,?,?)',
                            (uuid.uuid4().hex,snapshot_id,timestamp,final.name,len(files),json.dumps(files)))
                except Exception:
                    shutil.rmtree(final)
                    raise
                return {'folder': final.name, 'file_count': len(files), 'verified': True, 'created_at': timestamp}
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    def seed_demo(self):
        with self.lock:
            if not self.demo_allowed or any(self.source.iterdir()) or self.snapshots():
                raise ValueError('Demo initialization requires the empty default source and no snapshots')
            (self.source / 'config').mkdir()
            (self.source / 'config/app.conf').write_text('mode=demo\nport=8080\n')
            (self.source / 'notes.txt').write_text('Fictional demo files. No personal data.\n')
            (self.source / 'retired.txt').write_text('An example file removed in version two.\n')
            self.create('Demo · first working version')
            (self.source / 'config/app.conf').write_text('mode=demo\nport=8088\n')
            (self.source / 'retired.txt').unlink()
            (self.source / 'release-notes.txt').write_text('Demo version two: changed port, added release notes.\n')
            self.create('Demo · updated configuration')
            return {'created': 2}
