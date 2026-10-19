from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from core import TimeMachine
from server import handler_for


class MachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.machine = TimeMachine(self.root / 'data')

    def tearDown(self):
        self.tmp.cleanup()

    def create(self):
        (self.machine.source/'hello.txt').write_bytes(b'original\n')
        return self.machine.create('First version')['id']

    def test_create_verify_and_history_persistence(self):
        snapshot = self.create()
        self.assertTrue(self.machine.verify(snapshot)['verified'])
        reopened = TimeMachine(self.root/'data')
        self.assertEqual(reopened.snapshots()[0]['id'],snapshot)
        self.assertEqual(reopened.files(snapshot)['items'][0]['size'],9)

    def test_compare_added_changed_missing(self):
        first = self.create()
        (self.machine.source/'hello.txt').write_text('changed')
        (self.machine.source/'removed.txt').write_text('gone')
        second = self.machine.create()['id']
        (self.machine.source/'removed.txt').unlink()
        (self.machine.source/'added.txt').write_text('new')
        third = self.machine.create()['id']
        self.assertEqual(self.machine.compare(first,second)['counts']['changed'],1)
        result = self.machine.compare(second,third)
        self.assertEqual(result['counts']['added'],1)
        self.assertEqual(result['counts']['missing'],1)

    def test_selected_restore_preserves_source_and_checks_bytes(self):
        snapshot = self.create()
        (self.machine.source/'hello.txt').write_bytes(b'new source content')
        result = self.machine.restore(snapshot,['hello.txt'])
        recovered = self.machine.recovery/result['folder']/'hello.txt'
        self.assertEqual(recovered.read_bytes(),b'original\n')
        self.assertEqual((self.machine.source/'hello.txt').read_bytes(),b'new source content')
        self.assertTrue(result['verified'])
        self.assertEqual(self.machine.recovery_count(),1)

    def test_restore_uses_a_new_folder_each_time(self):
        snapshot = self.create()
        first = self.machine.restore(snapshot,['hello.txt'])
        second = self.machine.restore(snapshot,['hello.txt'])
        self.assertNotEqual(first['folder'],second['folder'])

    def test_bad_selection_does_not_write_recovery(self):
        snapshot = self.create()
        for names in [[],['../outside'],['missing'],['hello.txt','hello.txt']]:
            with self.assertRaises(ValueError):self.machine.restore(snapshot,names)
        self.assertFalse(any(self.machine.recovery.iterdir()))

    def test_corrupted_archive_cannot_be_restored(self):
        snapshot = self.create()
        item = self.machine.detail(snapshot)
        (self.machine.backups/item['archive']).write_bytes(b'not a tar archive')
        with self.assertRaises(ValueError):self.machine.restore(snapshot,['hello.txt'])
        self.assertEqual(self.machine.snapshots()[0]['verification_status'],'failed')
        self.assertEqual(self.machine.recovery_count(),0)

    def test_unlisted_archive_member_fails_verification(self):
        snapshot = self.create()
        item = self.machine.detail(snapshot)
        archive_path = self.machine.backups/item['archive']
        replacement = self.root/'replacement.tar.gz'
        with tarfile.open(archive_path) as source,tarfile.open(replacement,'w:gz') as target:
            for member in source.getmembers():target.addfile(member,source.extractfile(member) if member.isfile() else None)
            extra=tarfile.TarInfo('data/unlisted');extra.size=1;target.addfile(extra,io.BytesIO(b'x'))
        replacement.replace(archive_path)
        with self.assertRaises(ValueError):self.machine.verify(snapshot)

    def test_demo_only_modifies_empty_default_workspace(self):
        self.machine.seed_demo()
        self.assertEqual(len(self.machine.snapshots()),2)
        with self.assertRaises(ValueError):self.machine.seed_demo()
        custom = self.root/'custom';custom.mkdir()
        machine = TimeMachine(self.root/'other-data',custom)
        with self.assertRaises(ValueError):machine.seed_demo()
        self.assertFalse(any(custom.iterdir()))

    def test_data_cannot_be_inside_source(self):
        source=self.root/'parent';source.mkdir()
        with self.assertRaises(ValueError):TimeMachine(source/'data',source)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.machine = TimeMachine(Path(self.tmp.name)/'data')
        self.token = 'test-token'
        self.server = ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.machine,self.token))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()

    def request(self,path,body=None,headers=None):
        data=json.dumps(body).encode() if body is not None else None
        options={'Content-Type':'application/json','X-Time-Machine-Token':self.token}
        options.update(headers or {})
        request=Request(self.base+path,data=data,headers=options)
        try:response=urlopen(request,timeout=5)
        except HTTPError as error:response=error
        with response:
            result=response.read()
            return response.code,json.loads(result) if 'application/json' in response.headers['Content-Type'] else result

    def test_session_and_demo_snapshot_flow(self):
        code,state=self.request('/api/state')
        self.assertEqual(code,200);self.assertEqual(state['token'],self.token)
        self.assertEqual(self.request('/api/demo',{})[0],201)
        state=self.request('/api/state')[1]
        self.assertEqual(len(state['snapshots']),2)
        self.assertEqual(state['recovery_count'],0)

    def test_write_requires_token_and_expected_origin_and_host(self):
        self.assertEqual(self.request('/api/demo',{}, {'X-Time-Machine-Token':'wrong'})[0],403)
        self.assertEqual(self.request('/api/demo',{}, {'Origin':'https://other.invalid'})[0],403)
        self.assertEqual(self.request('/api/state',headers={'Host':'other.invalid'})[0],403)
        self.assertEqual(len(self.machine.snapshots()),0)

    def test_arbitrary_files_and_unknown_snapshot_not_served(self):
        self.assertEqual(self.request('/data/history.sqlite3')[0],404)
        self.assertEqual(self.request('/server.py')[0],404)
        self.assertEqual(self.request('/api/snapshots/'+'a'*32+'/files')[0],404)

    def test_disposable_http_lifecycle_smoke(self):
        from smoke_test import run
        run(self.base)
        folders=list(self.machine.recovery.glob('restore-*'))
        self.assertEqual(len(folders),1)
        self.assertEqual((folders[0]/'config/app.conf').read_text(),'mode=demo\nport=8080\n')
        self.assertEqual((self.machine.source/'config/app.conf').read_text(),'mode=demo\nport=8088\n')


if __name__=='__main__':unittest.main()
