import copy,json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from vco_studio.evidence import query,SCHEMA
from vco_studio.catalog import default_project,list_families
from vco_studio.generator import make_graph,validate_graph

class EvidenceTests(unittest.TestCase):
 def test_baseline_is_separate_real_bank(self):
  self.assertEqual(len(list_families()),5)
  g,p=make_graph(default_project('class_b'))
  self.assertTrue(validate_graph(g,p['binding'])['valid'])
  self.assertEqual(set(p['registers']),{'main'})
  self.assertFalse(any(i['role']=='mutual' for i in g['instances']))
  self.assertTrue(any(i.get('master')=='CT_R3_main' for i in g['instances']))
 @patch("vco_studio.evidence.audit_point",return_value="test_only")
 def test_conditional_query_excludes_false_comparisons(self,audit):
  base={'schema':SCHEMA,'id':'a','pdk':'demo_a','architecture':'class_b','status':'measured','frequency_hz':10e9,'power_mw':3,'vdd_v':1,'temp_c':27,'main_bits':5,'physical_id':'one','real_bank':True,'q_at_f0':25,'source':{'hashes':{'a':'hash'}},'controls':{'main':0},'fom_db':{'1000000':180}}
  rows=[base,dict(base,id='b',power_mw=6,fom_db={'1000000':200}),dict(base,id='c',real_bank=False),dict(base,id='d',status='failed'),dict(base,id='e',pdk='other'),dict(base,id='f',frequency_hz=20e9)]
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'measurements.jsonl';p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
   r=query(p,pdk='demo_a',frequency_hz=10e9,power_mw=3)
   self.assertEqual([x['id'] for x in r['matches']],['a'])
   self.assertEqual(r['observed_peak']['fom_db']['1000000'],180)
   self.assertEqual(query(p,pdk='demo_d')['status'],'unknown')
   self.assertEqual(query(p,pdk='invalid binding id')['status'],'needs_confirmation')
 @patch("vco_studio.evidence.audit_point",return_value="test_only")
 def test_no_physical_band_mixing(self,audit):
  base={'schema':SCHEMA,'id':'a','pdk':'demo_a','status':'measured','frequency_hz':10e9,'power_mw':3,'physical_id':'one','real_bank':True,'q_at_f0':25,'source':{'hashes':{'a':'x'}},'controls':{'main':0},'fom_db':{'1000000':180}}
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'m.jsonl';p.write_text('\n'.join(json.dumps(r) for r in [base,dict(base,id='b',physical_id='two',frequency_hz=15e9)]))
   r=query(p);self.assertEqual(len(r['sample_groups']),2)
   self.assertTrue(all(x['tr_fraction']==0 for x in r['sample_groups']))

class AuditTests(unittest.TestCase):
 def test_external_audit_closes_fom_and_detects_tamper(self):
  import math
  from vco_studio.evidence import audit_point
  from vco_studio.common import sha256
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);raw=root/'raw';raw.write_text('actual bytes');hashes={str(raw):sha256(raw)};audit=root/'audit.json'
   pn=-120;fom=-pn+20*math.log10(4e9/1e6)-10*math.log10(4)
   metrics={'frequency_hz':4e9,'power_mw':4,'pn_dbc_hz':{'1000000':pn},'fom_db':{'1000000':fom}}
   audit.write_text(json.dumps({'status':'audited','physical_id':'p','source':{'hashes':hashes},'metrics':metrics}))
   row=dict(metrics,physical_id='p',source={'run_dir':str(root),'hashes':hashes,'audit':str(audit)})
   self.assertEqual(audit_point(row),'external_audit_only')
   row['fom_db']['1000000']+=1
   with self.assertRaises(ValueError):audit_point(row)
   row['fom_db']['1000000']-=1;raw.write_text('tampered')
   with self.assertRaises(ValueError):audit_point(row)
 def test_band_requires_complete_declared_domain(self):
  from vco_studio.evidence import qualify_bands
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'bands.json';band={'physical_id':'p','coverage':'all_main_codes','point_ids':['a']};path.write_text(json.dumps([band]))
   point={'id':'a','physical_id':'p','main_bits':1,'controls':{'main':0},'frequency_hz':1e9,'fom_db':{'1000000':180}}
   accepted,rejected=qualify_bands([path],[point],'1000000');self.assertFalse(accepted);self.assertTrue(rejected)
   second=dict(point,id='b',controls={'main':1},frequency_hz=2e9,fom_db={'1000000':170});band['point_ids'].append('b');path.write_text(json.dumps([band]))
   accepted,rejected=qualify_bands([path],[point,second],'1000000');self.assertEqual(accepted[0]['worst_fom_db'],170)
 def test_tail_has_direct_current_source_and_no_aux(self):
  g,p=make_graph(default_project('class_b','tail'));by={i['name']:i for i in g['instances']}
  self.assertEqual(by['MB']['nodes']['d'],'S');self.assertNotIn('LT',by);self.assertNotIn('CB',by);self.assertEqual(set(p['registers']),{'main'})
 def test_same_named_model_different_source_requires_policy(self):
  from vco_studio.common import read_json,ROOT
  b=read_json(ROOT/'examples/bindings/demo_a.json');b['model_includes']=[{'path':'/tmp/other-process.scs','section':'tt'}]
  for role in b['roles'].values():role.pop('geometry_policy',None)
  with self.assertRaisesRegex(ValueError,'Same model name'):make_graph(default_project('class_b',binding=b))

class AliasTests(unittest.TestCase):
 def test_explicit_process_fullnames(self):
  from vco_studio.evidence import canonical_pdk
  self.assertEqual(canonical_pdk('DEMO_C'),'demo_c')
  self.assertEqual(canonical_pdk(' demo_d '),'demo_d')
  self.assertEqual(canonical_pdk('DeMo_A'),'demo_a')
  self.assertIsNone(canonical_pdk('some other experimental process'))


class UserEvidenceConfigurationTests(unittest.TestCase):
 def test_missing_user_data_stays_unknown_and_cards_remain_available(self):
  from vco_studio import evidence, knowledge, site
  config={'path_map':{}, 'legacy_prefixes':{}, 'kb':{'enabled':False}}
  with tempfile.TemporaryDirectory() as temp:
   config.update(evidence_indexes=[str(Path(temp)/'missing.jsonl')],
                 evidence_failure_files=[str(Path(temp)/'missing-failures.jsonl')])
   with patch.object(site,'load',return_value=config), patch.dict('os.environ',{},clear=True):
    data=evidence.compact(evidence.query(evidence.default_indexes(),pdk='demo_a',frequency_hz=4e9,power_mw=4))
    self.assertEqual(data['status'],'unknown')
    self.assertEqual(data['best_per_architecture'],[])
    self.assertEqual(data['attempted_failed_nearby'],[])
    consult=knowledge.consult(None,'tuning',{'symptoms':['PN_FLICKER']},query='tail flicker')
    self.assertTrue(consult['cards'])
    self.assertIn('not configured',consult['kb_error'])

 @patch('vco_studio.evidence.audit_point',return_value='test_only')
 def test_site_indexes_and_failure_ledgers_reach_query(self,audit):
  from vco_studio import evidence, site
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)
   point={'schema':SCHEMA,'id':'configured-point','pdk':'demo_a','architecture':'class_b',
          'status':'measured','frequency_hz':4e9,'power_mw':4,'vdd_v':1,'temp_c':27,
          'main_bits':4,'physical_id':'example','real_bank':True,'q_at_f0':20,
          'controls':{'main':0},'source':{'hashes':{'raw':'test-only'}},'fom_db':{'1000000':180}}
   (root/'measurements.jsonl').write_text(json.dumps(point)+'\n')
   failure={'pdk':'demo_a','architecture':'hegazi','variant':'tail',
            'target':{'frequency_ghz':4,'power_mw':4},'failure_class':'no_oscillation'}
   (root/'failures.jsonl').write_text('\n'.join(json.dumps(row) for row in (
       failure,dict(failure,pdk='demo_b'),dict(failure,target={'frequency_ghz':8,'power_mw':4}))))
   config={'path_map':{'measurements':str(root)},'legacy_prefixes':{},
           'evidence_indexes':['@measurements/measurements.jsonl'],
           'evidence_failure_files':['@measurements/missing.jsonl','@measurements/failures.jsonl']}
   with patch.object(site,'load',return_value=config):
    data=evidence.compact(evidence.query(evidence.default_indexes(),pdk='demo_a',frequency_hz=4e9,power_mw=4))
    self.assertEqual(data['status'],'measured_matches')
    self.assertEqual(data['best_per_architecture'][0]['id'],'configured-point')
    self.assertEqual(len(data['attempted_failed_nearby']),1)
    self.assertEqual(data['attempted_failed_nearby'][0]['failure_class'],'no_oscillation')
