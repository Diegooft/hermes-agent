import importlib.util, pathlib, unittest
spec=importlib.util.spec_from_file_location('supply',pathlib.Path(__file__).with_name('synapse_supply.py'))
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
 def test_malformed_graph_fails(self):
  with self.assertRaises(ValueError):list(m.eligible({'nodes':[]}))
 def test_same_source_stable_identity(self):
  self.assertEqual(m.identity('growth','G-1'),m.identity('growth','G-1'))
  self.assertNotEqual(m.identity('growth','G-1'),m.identity('voz','G-1'))
if __name__=='__main__':unittest.main()
