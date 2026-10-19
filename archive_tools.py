"""Create, verify, and restore file backups with SHA-256 manifests."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
import uuid

LOG = logging.getLogger('backup')
MAX_MANIFEST = 16 * 1024 * 1024


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name or str(path) != name or name == '.':
        raise ValueError(f'Unsafe archive path: {name}')
    return path


def digest(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
    return h.hexdigest()


def create(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise ValueError('Source must be a directory')
    if destination == source or source in destination.parents:
        raise ValueError('Backup destination must be outside the source tree')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = 'backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.tar.gz'
    final = destination / name
    fd, temporary = tempfile.mkstemp(prefix='.backup-', dir=destination)
    os.close(fd)
    manifest = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': {}, 'directories': []}
    try:
        with tarfile.open(temporary, 'w:gz') as archive:
            for parent, dirs, files in os.walk(source, followlinks=False):
                dirs.sort()
                files.sort()
                for directory in list(dirs):
                    path = Path(parent) / directory
                    if path.is_symlink():
                        LOG.warning('Skipped symlink: %s', path.relative_to(source))
                        dirs.remove(directory)
                        continue
                    rel = path.relative_to(source).as_posix()
                    manifest['directories'].append(rel)
                    info = tarfile.TarInfo('data/' + rel)
                    info.type, info.mode = tarfile.DIRTYPE, 0o700
                    archive.addfile(info)
                for filename in files:
                    path = Path(parent) / filename
                    # O_NOFOLLOW also rejects a file changed to a symlink between walk/open.
                    try:
                        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    except OSError:
                        if path.is_symlink():
                            LOG.warning('Skipped symlink: %s', path.relative_to(source))
                            continue
                        raise
                    with os.fdopen(handle, 'rb') as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ValueError('Only regular files and directories are supported')
                        rel = path.relative_to(source).as_posix()
                        safe_name(rel)
                        checksum = digest(stream)
                        stream.seek(0)
                        info = tarfile.TarInfo('data/' + rel)
                        info.size, info.mode = before.st_size, 0o600
                        archive.addfile(info, stream)
                        after = os.fstat(stream.fileno())
                        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            raise ValueError(f'Source changed during backup: {rel}')
                        manifest['files'][rel] = {'sha256': checksum, 'size': before.st_size}
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        # Verify the written bytes before making the archive visible as complete.
        verify(temporary)
        os.replace(temporary, final)
        LOG.info('Backup created: %s (%d files)', final, len(manifest['files']))
        return final
    finally:
        Path(temporary).unlink(missing_ok=True)


def inspect_archive(archive, max_bytes):
    members = {}
    total = 0
    for member in archive:
        safe_name(member.name)
        if member.name in members or not (member.isfile() or member.isdir()):
            raise ValueError('Duplicate path or unsupported archive entry')
        members[member.name] = member
        total += member.size
        if total > max_bytes or len(members) > 100000:
            raise ValueError('Archive exceeds configured size or entry limit')
    member = members.get('manifest.json')
    if member is None or not member.isfile() or member.size > MAX_MANIFEST:
        raise ValueError('Missing or oversized manifest')
    with archive.extractfile(member) as stream:
        manifest = json.load(stream)
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict) or not isinstance(manifest.get('directories'), list):
        raise ValueError('Unsupported manifest')
    expected = {'manifest.json'}
    for directory in manifest['directories']:
        safe_name(directory)
        name = 'data/' + directory
        if name in expected or name not in members or not members[name].isdir():
            raise ValueError('Directory manifest mismatch')
        expected.add(name)
    for name, record in manifest['files'].items():
        safe_name(name)
        key = 'data/' + name
        member = members.get(key)
        if key in expected or member is None or not member.isfile() or member.size != record['size']:
            raise ValueError('File manifest mismatch')
        with archive.extractfile(member) as stream:
            if digest(stream) != record['sha256']:
                raise ValueError(f'Checksum mismatch: {name}')
        expected.add(key)
    if set(members) != expected:
        raise ValueError('Archive contains unlisted entries')
    return manifest, members


def verify(path, max_bytes=10 * 1024**3):
    with tarfile.open(path, 'r:gz') as archive:
        manifest, _ = inspect_archive(archive, max_bytes)
    LOG.info('Verified: %s (%d files)', path, len(manifest['files']))
    return manifest


def restore(path, target, max_bytes=10 * 1024**3):
    # Do not resolve the final path: a dangling symlink must count as existing.
    target = Path(os.path.abspath(target))
    if target.exists() or target.is_symlink():
        raise ValueError('Restore target must not already exist')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.restore-', dir=target.parent))
    try:
        with tarfile.open(path, 'r:gz') as archive:
            manifest, members = inspect_archive(archive, max_bytes)
            for directory in manifest['directories']:
                (stage / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in manifest['files']:
                output = stage / name
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(members['data/' + name]) as src, output.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                output.chmod(0o600)
        # This tool is intended for directories controlled by the current user.
        if target.exists() or target.is_symlink():
            raise ValueError('Restore target appeared while restoring')
        stage.rename(target)
        LOG.info('Restore complete: %s', target)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-file', help='Append logs to a private local file')
    commands = parser.add_subparsers(dest='action', required=True)
    p = commands.add_parser('create')
    p.add_argument('source')
    p.add_argument('destination')
    for action in ('verify', 'restore'):
        p = commands.add_parser(action)
        p.add_argument('archive')
        if action == 'restore':
            p.add_argument('target')
        p.add_argument('--max-gib', type=float, default=10)
    args = parser.parse_args()
    os.umask(0o077)
    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    try:
        if args.action == 'create':
            print(create(args.source, args.destination))
        elif args.action == 'verify':
            verify(args.archive, args.max_gib * 1024**3)
        else:
            restore(args.archive, args.target, args.max_gib * 1024**3)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        LOG.error('%s failed: %s', args.action, error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
