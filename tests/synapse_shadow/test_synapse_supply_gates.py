import importlib.util, pathlib, unittest, tempfile, datetime, hashlib, subprocess
from unittest import mock
spec=importlib.util.spec_from_file_location('supply',pathlib.Path(__file__).resolve().parents[2]/'scripts'/'synapse_shadow'/'synapse_supply.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
NOW=datetime.datetime(2026,10,16,12,0,tzinfo=datetime.timezone.utc)
EPOCH=int(NOW.timestamp())
def G(req): return {'nodes':{'A':{'state':'TODO','requires_decision':req}}}
def D(**kw): return {'decisions':{'D1':{'status':'ratified','expires_at':None,**kw}}}
def keys(g,d,now=NOW): return [k for k,_ in m.eligible(g,decisions=d,now=now)]

class DecisionGate(unittest.TestCase):
 def test_ratified_not_expired_is_eligible(self):
  self.assertEqual(keys(G(['D1']),D()),['A'])
  self.assertEqual(keys(G(['D1']),D(expires_at=datetime.date(2026,12,1))),['A'])
 def test_not_ratified_ineligible(self):
  for st in ['proposed','pending','rejected','',None]:
   self.assertEqual(keys(G(['D1']),{'decisions':{'D1':{'status':st,'expires_at':None}}}),[],st)
 def test_ratified_but_expired_ineligible(self):
  self.assertEqual(keys(G(['D1']),D(expires_at=datetime.date(2026,10,1))),[])
  self.assertEqual(keys(G(['D1']),D(expires_at='2026-10-16T11:59:00+00:00')),[])
 def test_unknown_or_malformed_decision_ineligible(self):
  self.assertEqual(keys(G(['NOPE']),D()),[])
  self.assertEqual(keys(G(['D1']),{'decisions':[]}),[])
  self.assertEqual(keys(G(['D1']),D(expires_at='lixo')),[])
 def test_all_required_decisions_must_hold(self):
  d={'decisions':{'D1':{'status':'ratified','expires_at':None},'D2':{'status':'proposed','expires_at':None}}}
  self.assertEqual(keys(G(['D1','D2']),d),[])
 def test_without_decisions_historic_behaviour(self):
  self.assertEqual(list(m.eligible(G(['D1']))),[])
  self.assertEqual([k for k,_ in m.eligible(G([]))],['A'])

def card(**kw): return {'status':'running','body':'fonte: growth:G-1','claim_lock':'w1','claim_expires':EPOCH+600,**kw}
class ClaimLiveness(unittest.TestCase):
 def test_live_claim(self): self.assertEqual(m.claim_state(card(),NOW),'LIVE')
 def test_expired_claim_stale(self): self.assertEqual(m.claim_state(card(claim_expires=EPOCH-1),NOW),'STALE')
 def test_no_claim(self): self.assertEqual(m.claim_state(card(claim_lock=None,claim_expires=None),NOW),'NONE')
 def test_claim_without_expiry_is_stale_not_live(self): self.assertEqual(m.claim_state(card(claim_expires=None),NOW),'STALE')
 def test_garbage_expiry_stale(self): self.assertEqual(m.claim_state(card(claim_expires='x'),NOW),'STALE')
 def test_duplicate_detail_names_stale_claim(self):
  n={'state':'TODO'}
  self.assertEqual(m.duplicate_detail('growth','G-1',n,[card()],[],NOW),'DUP_LIVE')
  self.assertEqual(m.duplicate_detail('growth','G-1',n,[card(claim_expires=EPOCH-5)],[],NOW),'DUP_STALE_CLAIM')
  self.assertEqual(m.duplicate_detail('growth','G-1',n,[card(status='todo',claim_lock=None,claim_expires=None)],[],NOW),'DUP_LIVE')
  self.assertIsNone(m.duplicate_detail('growth','G-1',n,[{'body':'growth:G-10','status':'todo'}],[],NOW))
 def test_stale_claim_still_blocks_duplicate(self):
  self.assertTrue(m.duplicate('growth','G-1',{'state':'TODO'},[card(claim_expires=EPOCH-5)],[]))
 def test_stale_claims_listed(self):
  self.assertEqual(len(m.stale_claims([card(),card(claim_expires=EPOCH-5)],NOW)),1)

class SourceGates(unittest.TestCase):
 def setUp(self):
  self._temp=tempfile.TemporaryDirectory();self.d=pathlib.Path(self._temp.name)
 def tearDown(self): self._temp.cleanup()
 def test_pinned_read_and_verify_ok(self):
  p=self.d/'g.yaml';p.write_bytes(b'nodes: {}\n')
  data,sha=m.read_pinned(p)
  self.assertEqual(sha,hashlib.sha256(b'nodes: {}\n').hexdigest()); m.verify_pinned(p,sha)
 def test_mutated_source_refused(self):
  p=self.d/'g.yaml';p.write_bytes(b'a: 1\n');_,sha=m.read_pinned(p);p.write_bytes(b'a: 2\n')
  with self.assertRaises(m.Refusal) as c: m.verify_pinned(p,sha)
  self.assertEqual((c.exception.code,c.exception.klass),('FONTE_MUTADA','INFRA'))
  self.assertEqual(c.exception.measured['esperado'],sha)
 def test_deleted_source_refused_named(self):
  p=self.d/'g.yaml';p.write_bytes(b'a: 1\n');_,sha=m.read_pinned(p);p.unlink()
  with self.assertRaises(m.Refusal) as c: m.verify_pinned(p,sha)
  self.assertEqual(c.exception.code,'FONTE_INDISPONIVEL')
 def test_unavailable_source_never_empty(self):
  with self.assertRaises(m.Refusal) as c: m.read_pinned(self.d/'nao-existe.yaml')
  self.assertEqual((c.exception.code,c.exception.klass),('FONTE_INDISPONIVEL','INFRA'))
  self.assertIn('caminho',c.exception.measured)
 def test_git_source_timeout_is_a_named_infrastructure_refusal(self):
  p=self.d/'g.yaml';p.write_bytes(b'nodes: {}\n')
  with mock.patch.object(m.subprocess,'run',side_effect=subprocess.TimeoutExpired('git',10)):
   with self.assertRaises(m.Refusal) as c:m.read_versioned(p,repo=self.d)
  self.assertEqual((c.exception.code,c.exception.klass),('REVISAO_INDISPONIVEL','INFRA'))
 def test_empty_source_is_refusal_not_nothing_to_do(self):
  p=self.d/'e.yaml';p.write_bytes(b'')
  with self.assertRaises(m.Refusal) as c: m.read_pinned(p)
  self.assertEqual(c.exception.code,'FONTE_VAZIA')
 def test_refusal_record_is_dict_with_class(self):
  r=m.Refusal('FONTE_INDISPONIVEL','INFRA',{'caminho':'x'}).record()
  self.assertEqual((r['status'],r['codigo'],r['classe']),('refused','FONTE_INDISPONIVEL','INFRA'))
 def test_dirty_tracked_graph_cannot_change_eligibility(self):
  repo=self.d/'repo';gov=repo/'governance';gov.mkdir(parents=True)
  (gov/'programs.yaml').write_text('programs:\n  fixture:\n    status: active\n    graph: governance/graph.yaml\n',encoding='utf-8')
  graph=gov/'graph.yaml'
  graph.write_text('nodes:\n  PARENT:\n    state: IN_PROGRESS\n  CHILD:\n    state: BLOCKED_BY_DEPENDENCY\n    depends_on: [PARENT]\n',encoding='utf-8')
  subprocess.run(['git','init','-q'],cwd=repo,check=True,capture_output=True)
  subprocess.run(['git','config','user.name','C05 test'],cwd=repo,check=True,capture_output=True)
  subprocess.run(['git','config','user.email','c05-test@example.invalid'],cwd=repo,check=True,capture_output=True)
  subprocess.run(['git','add','governance/programs.yaml','governance/graph.yaml'],cwd=repo,check=True,capture_output=True)
  subprocess.run(['git','commit','-q','-m','fixture'],cwd=repo,check=True,capture_output=True)
  with mock.patch.object(m,'REPO',repo),mock.patch.object(m,'HOME',self.d/'empty-home'),mock.patch.object(m,'SOURCE_PROVENANCE_BY_SOURCE',{}):
   self.assertEqual(list(m.candidates()),[])
  graph.write_text('nodes:\n  PARENT:\n    state: DONE\n  CHILD:\n    state: TODO\n    depends_on: [PARENT]\n',encoding='utf-8')
  with mock.patch.object(m,'REPO',repo),mock.patch.object(m,'HOME',self.d/'empty-home'),mock.patch.object(m,'SOURCE_PROVENANCE_BY_SOURCE',{}):
   with self.assertRaises(m.Refusal) as c:list(m.candidates())
  self.assertIn(c.exception.code,{'CHECKOUT_SUJO','FONTE_MUTADA'})
if __name__=='__main__':unittest.main()
