"""Delivery-mode invariants through native SQLite lifecycle and loopback GitHub evidence."""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli.kanban_db_connect import connect

@pytest.fixture
def delivery_github(tmp_path, monkeypatch):
    state = dict(base='staging', state='OPEN', merged=False, conclusion='success', head='a'*40, requests=[])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'].append(self.path)
            if self.path == '/graphql':
                value={'data':{'repository':{'pullRequest':{'headRefOid':state['head'],
                    'baseRefName':state['base'],'state':state['state'],
                    'baseRef':{'branchProtectionRule':{'requiredStatusChecks':[{'context':'required','app':{'databaseId':1}}]}}}}}}
            elif '/rules/branches/' in self.path: value=[[]]
            elif '/check-runs' in self.path:
                value=[{'total_count':1,'check_runs':[{'id':42,'name':'required','head_sha':state['head'],
                    'app':{'id':1},'status':'completed','conclusion':state['conclusion'],'html_url':'https://github.com/acme/repo/actions/runs/42'}]}]
                if state.get('race'): state['race']()
            elif '/statuses' in self.path: value=[[]]
            elif '/pulls/' in self.path:
                value={'head':{'sha':state['head']},'base':{'ref':state['base']},
                    'state':'closed' if state['merged'] else 'open','merged':state['merged']}
            else:self.send_error(404);return
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def api(endpoint,**kwargs):
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/'+endpoint,timeout=5) as response:
            return json.load(response)
    monkeypatch.setattr(acceptance,'_api',api)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'home'));kb.init_db()
    try:yield state
    finally:server.shutdown();server.server_close();thread.join()

def test_delivery_mode_requires_merged_staging_and_preserves_legacy(delivery_github):
    state=delivery_github
    with connect() as conn:
        for base,pr_state,merged,conclusion,expected in [
            ('main','MERGED',True,'success',False),('staging','OPEN',False,'success',False),
            ('staging','MERGED',False,'success',False),('staging','MERGED',True,'failure',False),
            ('staging','MERGED',True,'success',True)]:
            state.update(base=base,state=pr_state,merged=merged,conclusion=conclusion)
            task=kb.create_task(conn,title='Delivery',completion_contract='github_pr')
            assert kb.complete_task(conn,task,result='Evidence',metadata={'published_pr':'https://github.com/acme/repo/pull/7'}) is expected
            assert (kb.get_task(conn,task).status=='done') is expected
            assert kb.get_task(conn,task).completion_contract=='github_pr'
        state.update(base='main',state='OPEN',merged=False,conclusion='success')
        legacy=kb.create_task(conn,title='Legacy',completion_contract='acme/repo')
        assert kb.complete_task(conn,legacy,result='Evidence',metadata={'published_pr':'https://github.com/acme/repo/pull/7'})
        reads=len(state['requests'])
        local=kb.create_task(conn,title='Local',completion_contract='local-only')
        assert kb.complete_task(conn,local,summary='Local acceptance')
        assert len(state['requests'])==reads

def test_delivery_publication_binds_once_and_run_race_cannot_complete(delivery_github):
    state=delivery_github
    with connect() as conn:
        task=kb.create_task(conn,title='Bind',completion_contract='github_pr')
        assert not kb.complete_task(conn,task,result='Published',metadata={'published_pr':'https://github.com/acme/repo/pull/7'})
        state.update(state='MERGED',merged=True)
        reads=len(state['requests'])
        assert not kb.complete_task(conn,task,result='Sibling',metadata={'published_pr':'https://github.com/acme/repo/pull/8'})
        assert len(state['requests'])==reads
        assert kb.complete_task(conn,task,result='Merged',metadata={'published_pr':'https://github.com/acme/repo/pull/7'})
        raced=kb.create_task(conn,title='Race',completion_contract='github_pr')
        run_id=kb.claim_task(conn,raced).current_run_id
        def reclaim():
            with connect() as rival:
                assert kb.block_task(rival,raced,reason='Owner reassigned');assert kb.unblock_task(rival,raced)
                kb.claim_task(rival,raced)
        state['race']=reclaim
        assert not kb.complete_task(conn,raced,result='Merged',expected_run_id=run_id,
            metadata={'published_pr':'https://github.com/acme/repo/pull/7'})
        assert kb.get_task(conn,raced).status!='done'
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",(raced,)).fetchone()[0]==0