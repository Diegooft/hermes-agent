import contextlib, importlib.util, io, json, pathlib, tempfile, unittest
from unittest import mock
spec=importlib.util.spec_from_file_location('supply',pathlib.Path(__file__).resolve().parents[2]/'scripts'/'synapse_shadow'/'synapse_supply.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class SupplyTest(unittest.TestCase):
 def test_next_eligible_and_dependency(self):
  graph={'nodes':{'A':{'state':'DONE'},'B':{'state':'TODO','depends_on':['A']},'C':{'state':'TODO','depends_on':['missing']}}}
  self.assertEqual([x[0] for x in m.eligible(graph)],['B'])
 def test_never_rebuild_running_blocked_unknown_or_decision(self):
  for state in ['DONE','IN_PROGRESS','BLOCKED','PARKED_EXTERNAL_RUNTIME_EVIDENCE','unexpected']:
   self.assertEqual(list(m.eligible({'nodes':{'A':{'state':state}}})),[])
  self.assertEqual(list(m.eligible({'nodes':{'A':{'state':'TODO','requires_decision':['D']}}})),[])
 def test_card_and_pr_dedup(self):
  node={'state':'TODO','pr':42}
  self.assertTrue(m.duplicate('growth','G-1',node,[{'body':'fonte: growth:G-1'}],[]))
  self.assertTrue(m.duplicate('growth','G-1',node,[],[{'number':42}]))
  self.assertFalse(m.duplicate('growth','G-1',node,[{'body':'growth:G-10'}],[]))
 def test_fresh_supply_threshold(self):
  self.assertEqual(m.backlog_count([{'status':'todo'},{'status':'triage'},{'status':'blocked'},{'status':'done'},{'status':'running'},{'status':'review'}]),2)
 def test_dependency_block_can_be_supplied_after_verified_parents(self):
  graph={'nodes':{'A':{'state':'DONE'},'B':{'state':'BLOCKED_BY_DEPENDENCY','depends_on':['A']},'C':{'state':'BLOCKED_BY_DEPENDENCY'}}}
  self.assertEqual([x[0] for x in m.eligible(graph)],['B'])
 def test_code_landed_does_not_satisfy_acceptance_dependency(self):
  graph={'nodes':{'A':{'state':'CODE_LANDED_IN_STAGING'},'B':{'state':'TODO','depends_on':['A']}}}
  self.assertEqual(list(m.eligible(graph)),[])
 def test_certified_without_canonical_acceptance_does_not_release_dependency(self):
  graph={'nodes':{'A':{'state':'CERTIFIED'},'B':{'state':'TODO','depends_on':['A']}}}
  self.assertEqual(list(m.eligible(graph)),[])
 def test_malformed_graph_fails(self):
  with self.assertRaises(ValueError):list(m.eligible({'nodes':[]}))
 def test_same_source_stable_identity(self):
  self.assertEqual(m.identity('growth','G-1'),m.identity('growth','G-1'))
  self.assertNotEqual(m.identity('growth','G-1'),m.identity('voz','G-1'))
 def test_dry_run_does_not_create_supply_lock(self):
  with tempfile.TemporaryDirectory() as raw_home:
   home=pathlib.Path(raw_home)
   (home/'continuity.yaml').write_text('supply:\n  board: fixtures\n  low_watermark: 10\n',encoding='utf-8')
   output=io.StringIO()
   m.SOURCE_PROVENANCE_BY_SOURCE={'fixture.yaml':{'revision':'a'*40,'inputs':[{'source_kind':'git_blob','path':'fixture.yaml','commit':'a'*40,'sha256':'b'*64}]}}
   with mock.patch.multiple(
    m,
    HOME=home,
    verify_source_provenance=mock.Mock(),
    snapshot=mock.Mock(return_value=([],{'fixtures':[]})),
    open_prs=mock.Mock(return_value=[]),
    candidates=mock.Mock(return_value=iter([('program','NODE-1',{'name':'Candidate'},'fixture.yaml')])),
    native=mock.Mock(side_effect=AssertionError('dry-run must not write native cards')),
   ), mock.patch('sys.argv',['synapse_supply.py','--dry-run']), contextlib.redirect_stdout(output):
    m.main()
   self.assertIn('candidate',output.getvalue())
   self.assertFalse((home/'state'/'synapse-supply').exists())
 def test_receipt_and_native_card_body_bind_source_provenance(self):
  with tempfile.TemporaryDirectory() as raw_home:
   home=pathlib.Path(raw_home)
   (home/'continuity.yaml').write_text('supply:\n  board: fixtures\n  low_watermark: 10\n',encoding='utf-8')
   (home/'state'/'synapse-supply').mkdir(parents=True)
   source='governance/graph.yaml'
   provenance={'revision':'a'*40,'inputs':[{'source_kind':'git_blob','path':source,'commit':'a'*40,'sha256':'b'*64}]}
   native=mock.Mock(return_value={'id':'card-1','status':'triage'})
   output=io.StringIO()
   m.SOURCE_PROVENANCE_BY_SOURCE={source:provenance}
   with mock.patch.multiple(
    m,
    HOME=home,
    verify_source_provenance=mock.Mock(),
    lock=mock.Mock(side_effect=lambda _:contextlib.nullcontext()),
    snapshot=mock.Mock(return_value=([],{'fixtures':[]})),
    open_prs=mock.Mock(return_value=[]),
    candidates=mock.Mock(return_value=iter([('program','NODE-1',{'name':'Candidate','files':['src/a.py'],'acceptance':['local']},source)])),
    native=native,
   ), mock.patch('sys.argv',['synapse_supply.py']), contextlib.redirect_stdout(output):
    m.main()
   self.assertIn('b'*64,native.call_args.args[1])
   receipt=json.loads(next((home/'state'/'synapse-supply').glob('*.json')).read_text(encoding='utf-8'))
   self.assertEqual(receipt['source_provenance'],provenance)
if __name__=='__main__':unittest.main()
