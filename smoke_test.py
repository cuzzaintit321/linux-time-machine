"""Exercise an empty disposable tracker workspace over HTTP."""
import argparse
import json
from urllib.request import Request,urlopen


def run(base):
    def request(path,body=None,token=''):
        data=json.dumps(body).encode() if body is not None else None
        req=Request(base.rstrip('/')+path,data=data,headers={'Content-Type':'application/json','X-Time-Machine-Token':token})
        with urlopen(req,timeout=30) as response:return json.load(response)
    state=request('/api/state')
    if not state['demo_available']:
        raise RuntimeError('Smoke test requires an empty default workspace; existing data was not modified')
    token=state['token']
    request('/api/demo',{},token)
    state=request('/api/state');latest=state['snapshots'][0]['id'];older=state['snapshots'][1]['id']
    comparison=request('/api/compare?before='+older+'&after='+latest)
    assert comparison['counts']['changed']==1 and comparison['counts']['added']==1 and comparison['counts']['missing']==1
    assert request('/api/snapshots/'+latest+'/verify',{},token)['verified']
    result=request('/api/snapshots/'+older+'/restore',{'files':['config/app.conf']},token)
    assert result['verified'] and result['file_count']==1
    assert request('/api/state')['recovery_count']==1
    print('PASS: demo snapshots, comparison, verification, selected restore, and recovery history')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8780')
    run(parser.parse_args().base_url)
