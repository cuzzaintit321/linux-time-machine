"""Local-only dashboard for verified file snapshots and recovery."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit
from core import TimeMachine

ROOT = Path(__file__).parent / 'web'
LOG = logging.getLogger('time-machine')


def handler_for(machine, token):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload, kind='application/json; charset=utf-8'):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            host = self.headers.get('Host', '')
            return host in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}

        def do_GET(self):
            if not self.allowed_host():
                self.reply(403, {'error': 'Use localhost with the configured port'})
                return
            parsed = urlsplit(self.path)
            try:
                assets = {'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
                if parsed.path in assets:
                    filename, kind = assets[parsed.path]
                    self.reply(200, (ROOT / filename).read_bytes(), kind)
                elif parsed.path == '/api/state':
                    self.reply(200, {'token':token,'snapshots':machine.snapshots(),'recoveries':machine.recoveries(),'recovery_count':machine.recovery_count(),
                        'demo_available':machine.demo_allowed and not any(machine.source.iterdir()) and not machine.snapshots(),
                        'recovery_location':'DATA_DIRECTORY/recovery/'})
                elif match := re.fullmatch(r'/api/snapshots/([a-f0-9]{32})/files', parsed.path):
                    self.reply(200, machine.files(match.group(1)))
                elif parsed.path == '/api/compare':
                    args = parse_qs(parsed.query)
                    self.reply(200, machine.compare(args.get('before',[''])[0],args.get('after',[''])[0]))
                elif parsed.path == '/health':
                    machine.snapshots()
                    self.reply(200, {'status':'ready'})
                else:
                    self.reply(404, {'error':'Not found'})
            except KeyError:
                self.reply(404, {'error':'Snapshot not found'})
            except (OSError, ValueError, sqlite3.Error) as error:
                LOG.warning('Read failed: %s',type(error).__name__)
                self.reply(503, {'error':'Local storage unavailable'})

        def do_POST(self):
            host = self.headers.get('Host','')
            origin = self.headers.get('Origin')
            supplied = self.headers.get('X-Time-Machine-Token','')
            if not self.allowed_host() or (origin and origin != 'http://' + host) or not hmac.compare_digest(supplied,token):
                self.reply(403, {'error':'Session check failed; reload this local dashboard'})
                return
            if self.headers.get('Content-Type','').split(';')[0].strip() != 'application/json':
                self.reply(415, {'error':'Use application/json'})
                return
            self.connection.settimeout(5)
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 262144:
                    self.reply(413, {'error':'Request too large or empty'})
                    return
                body = json.loads(self.rfile.read(size))
                if not isinstance(body,dict):
                    raise ValueError('Object required')
            except (ValueError, UnicodeDecodeError, OSError):
                self.reply(400, {'error':'Invalid JSON request'})
                return
            try:
                if self.path == '/api/snapshots':
                    self.reply(201,machine.create(body.get('label','Manual snapshot')))
                elif self.path == '/api/demo':
                    self.reply(201,machine.seed_demo())
                elif match := re.fullmatch(r'/api/snapshots/([a-f0-9]{32})/(verify|restore)',self.path):
                    snapshot_id, action = match.groups()
                    self.reply(200,machine.verify(snapshot_id) if action=='verify' else machine.restore(snapshot_id,body.get('files')))
                else:
                    self.reply(404, {'error':'Not found'})
            except KeyError:
                self.reply(404, {'error':'Snapshot not found'})
            except ValueError as error:
                self.reply(400, {'error':str(error)})
            except Exception:
                LOG.exception('Snapshot operation failed')
                self.reply(503, {'error':'Operation failed; check terminal logs and disk space'})

        def log_message(self, fmt, *args):
            return
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',default='data')
    parser.add_argument('--source',help='Existing directory to back up; configured once at startup')
    parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--demo',action='store_true',help='Create two fictional snapshots in an empty default workspace')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    machine = TimeMachine(args.data,args.source)
    if args.demo:
        machine.seed_demo()
    server = ThreadingHTTPServer((args.host,args.port),handler_for(machine,secrets.token_hex(32)))
    print(f'Linux Time Machine: http://127.0.0.1:{args.port}',flush=True)
    print(f'Recovery folders: {machine.recovery}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
