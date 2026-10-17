import contextlib, hashlib, importlib.util, io, json, pathlib, subprocess, tempfile, unittest
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
 def test_retry_recovers_native_card_after_lost_ack_without_second_dispatch(self):
  self._assert_retry_recovers_native_card('lost_ack')
 def test_retry_recovers_native_card_after_receipt_replace_failure(self):
  self._assert_retry_recovers_native_card('lost_receipt')
 def test_duplicate_without_unique_exact_key_and_provenance_is_not_recovered(self):
  for persisted in ('body_mismatch','multiple_matches'):
   with self.subTest(persisted=persisted), tempfile.TemporaryDirectory() as raw_home:
    home=pathlib.Path(raw_home)
    state=home/'state'/'synapse-supply';state.mkdir(parents=True)
    (home/'continuity.yaml').write_text('supply:\n  board: fixtures\n  low_watermark: 10\n',encoding='utf-8')
    source='governance/graph.yaml';provenance={'revision':'a'*40,'inputs':[{'source_kind':'git_blob','path':source,'commit':'a'*40,'sha256':'b'*64}]}
    m.SOURCE_PROVENANCE_BY_SOURCE={source:provenance}
    node={'name':'Candidate','files':['src/a.py'],'acceptance':['local']};body=m.body_for('program','NODE-1',node,source)
    cards=[{'id':'card-1','title':'Candidate','body':'different provenance','status':'triage','idempotency_key':m.identity('program','NODE-1')}]
    if persisted=='multiple_matches':
     cards=[{'id':f'card-{index}','title':'Candidate','body':body,'status':'triage','idempotency_key':m.identity('program','NODE-1')} for index in (1,2)]
    native=mock.Mock(side_effect=AssertionError('unsafe duplicate must not create another native card'))
    output=io.StringIO()
    with mock.patch.multiple(
     m,
     HOME=home,
     verify_source_provenance=mock.Mock(),
     lock=mock.Mock(side_effect=lambda _:contextlib.nullcontext()),
     snapshot=mock.Mock(return_value=(cards,{'fixtures':cards})),
     open_prs=mock.Mock(return_value=[]),
     candidates=mock.Mock(return_value=iter([('program','NODE-1',node,source)])),
     native=native,
    ), mock.patch('sys.argv',['synapse_supply.py']), contextlib.redirect_stdout(output):
     m.main()
    self.assertIn('no_eligible_unduplicated_node',output.getvalue())
    self.assertFalse(list(state.glob('*.json')))
    native.assert_not_called()
 def _assert_retry_recovers_native_card(self,failure_mode):
  with tempfile.TemporaryDirectory() as raw_home:
   home=pathlib.Path(raw_home)
   state=home/'state'/'synapse-supply';state.mkdir(parents=True)
   (home/'continuity.yaml').write_text('supply:\n  board: fixtures\n  low_watermark: 10\n',encoding='utf-8')
   source='governance/graph.yaml';provenance={'revision':'a'*40,'inputs':[{'source_kind':'git_blob','path':source,'commit':'a'*40,'sha256':'b'*64}]}
   persisted=[];native_calls=[];outputs=[]
   original_replace=m.os.replace;replace_attempted=False
   def snapshot():return list(persisted),{'fixtures':list(persisted)}
   def native(args,body=None):
    native_calls.append(args)
    card={'id':'card-1','title':'Candidate','body':body,'status':'triage','idempotency_key':m.identity('program','NODE-1')}
    if not persisted:persisted.append(card)
    if failure_mode=='lost_ack':raise RuntimeError('simulated lost native ACK after commit')
    return card
   def fail_replace_once(source_path,target_path):
    nonlocal replace_attempted
    if failure_mode=='lost_receipt' and not replace_attempted:
     replace_attempted=True
     raise OSError('simulated derived receipt replace failure')
    return original_replace(source_path,target_path)
   m.SOURCE_PROVENANCE_BY_SOURCE={source:provenance}
   for _ in range(2):
    output=io.StringIO();outputs.append(output)
    with mock.patch.multiple(
     m,
     HOME=home,
     verify_source_provenance=mock.Mock(),
     lock=mock.Mock(side_effect=lambda _:contextlib.nullcontext()),
     snapshot=snapshot,
     open_prs=mock.Mock(return_value=[]),
     candidates=mock.Mock(return_value=iter([('program','NODE-1',{'name':'Candidate','files':['src/a.py'],'acceptance':['local']},source)])),
     native=native,
    ), mock.patch('sys.argv',['synapse_supply.py']), contextlib.redirect_stdout(output):
     replace_context=mock.patch.object(m.os,'replace',side_effect=fail_replace_once) if failure_mode=='lost_receipt' else contextlib.nullcontext()
     with replace_context:
      if _==0 and failure_mode=='lost_ack':
       with self.assertRaisesRegex(RuntimeError,'simulated lost native ACK after commit'):m.main()
      elif _==0:
       with self.assertRaisesRegex(OSError,'simulated derived receipt replace failure'):m.main()
      else:m.main()
   receipt_path=state/(hashlib.sha256(m.identity('program','NODE-1').encode()).hexdigest()+'.json')
   self.assertEqual(len(native_calls),1)
   self.assertEqual(native_calls[0][native_calls[0].index('--idempotency-key')+1],m.identity('program','NODE-1'))
   self.assertEqual(len(persisted),1)
   self.assertTrue(receipt_path.exists(),outputs[-1].getvalue())
   receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
   self.assertEqual(receipt['card_id'],'card-1')
   self.assertEqual(receipt['source'],'program:NODE-1')
 def test_test_card_binds_its_embedded_fixture_to_the_script_blob(self):
  with tempfile.TemporaryDirectory() as raw_home:
   home=pathlib.Path(raw_home)
   runtime_home=home/'runtime';runtime_home.mkdir()
   (runtime_home/'continuity.yaml').write_text('supply:\n  board: fixtures\n  low_watermark: 10\n',encoding='utf-8')
   (runtime_home/'state'/'synapse-supply').mkdir(parents=True)
   hermes_repo=home/'hermes';script=hermes_repo/'scripts'/'synapse_shadow'/'synapse_supply.py';script.parent.mkdir(parents=True)
   script_bytes=b'embedded C05.2 test-card fixture source\n';script.write_bytes(script_bytes)
   subprocess.run(['git','init','--quiet',str(hermes_repo)],check=True)
   subprocess.run(['git','-C',str(hermes_repo),'config','user.name','Hermes test'],check=True)
   subprocess.run(['git','-C',str(hermes_repo),'config','user.email','hermes-test@example.invalid'],check=True)
   subprocess.run(['git','-C',str(hermes_repo),'add','--','scripts/synapse_shadow/synapse_supply.py'],check=True)
   subprocess.run(['git','-C',str(hermes_repo),'commit','--quiet','-m','fixture'],check=True)
   head=subprocess.check_output(['git','-C',str(hermes_repo),'rev-parse','HEAD'],text=True).strip()
   expected_sha=hashlib.sha256(script_bytes).hexdigest()
   native=mock.Mock(return_value={'id':'test-card-1','status':'triage'})
   output=io.StringIO()
   with mock.patch.multiple(
    m,
    HOME=runtime_home,
    REPO=home/'synapse-root',
    lock=mock.Mock(side_effect=lambda _:contextlib.nullcontext()),
    snapshot=mock.Mock(return_value=([],{'teste':[]})),
    open_prs=mock.Mock(return_value=[]),
    native=native,
   ), mock.patch.object(m,'__file__',str(script)), mock.patch('sys.argv',['synapse_supply.py','--test-card']), contextlib.redirect_stdout(output):
    m.main()
   self.assertIn(expected_sha,native.call_args.args[1])
   receipt=json.loads(next((runtime_home/'state'/'synapse-supply').glob('*.json')).read_text(encoding='utf-8'))
   provenance=receipt['source_provenance']
   self.assertEqual(provenance['revision'],head)
   self.assertEqual(provenance['inputs'][0]['commit'],head)
   self.assertEqual(provenance['inputs'][0]['sha256'],expected_sha)
   self.assertEqual(pathlib.Path(provenance['inputs'][0]['repository_root']),hermes_repo.resolve())
   m.verify_source_provenance(provenance)
if __name__=='__main__':unittest.main()
