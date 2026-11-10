import copy,json,tempfile,unittest
from pathlib import Path
from vco_studio.catalog import list_families,default_project
from vco_studio.generator import make_graph,validate_graph,generate
from vco_studio.characterize import bank_ac

class GeneratorTests(unittest.TestCase):
 def test_all_variants_and_independent_widths(self):
  for fam in list_families():
   for v in fam['variants']:
    p=default_project(fam['id'],v['id']);p['registers']['main']={'bits':6,'value':43}
    if 'aux' in p['registers']:p['registers']['aux']={'bits':3,'value':5}
    g,p=make_graph(p);self.assertTrue(validate_graph(g,p['binding'])['valid'])
    self.assertEqual(sum(x['name'].startswith('main_B') for x in g['ports']),6)
    bank=next(x for x in g['subcircuits'] if x['name']=='CT_R3_main');self.assertEqual(len(bank['instances']),63)
    self.assertEqual('aux' in p['registers'],fam['id'] not in ['guo','du'])
 def test_fixed_cap_and_invalid_register_rejected(self):
  p=default_project('murphy','nmos');g,_=make_graph(p)
  inst=next(x for x in g['instances'] if x['role']=='cap' and x['parameters'].get('c')==50e-15)
  p['overrides']={g['name']+'/'+inst['name']:{'c':60e-15}}
  with self.assertRaisesRegex(ValueError,'50fF'):make_graph(p)
  p=default_project('du');p['registers']['aux']={'bits':4,'value':0}
  with self.assertRaisesRegex(ValueError,'no independent'):make_graph(p)
 def test_physical_identity_codes_settings_and_geometry(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=default_project('murphy','nmos');a=generate(p,root/'a')
   q=copy.deepcopy(p);q['registers']['main']['value']=5;q['parameters']['frequency_guess_ghz']=11;q['simulation']['harmonics']=21
   b=generate(q,root/'b');self.assertEqual(a['physical_identity'],b['physical_identity'])
   q['parameters']['vdd']=.9;c=generate(q,root/'c');self.assertNotEqual(a['physical_identity'],c['physical_identity'])
   (root/'a'/'circuit.scs').write_text('modified')
   with self.assertRaisesRegex(ValueError,'changed after manifest'):bank_ac(root/'a',root/'bad')
 def test_target_seed_then_scale_and_override(self):
  p=default_project('murphy','nmos',str(Path(__file__).resolve().parents[1]/'examples/bindings/demo_b.json'))
  g,_=make_graph(p);bank=next(x for x in g['subcircuits'] if x['name']=='CT_R3U_main');cap=next(x for x in bank['instances'] if x['role']=='cap');base=cap['parameters']['l']
  p['parameters']['bank_cap_scale']=1.1;h,r=make_graph(p);cap2=next(x for x in next(y for y in h['subcircuits'] if y['name']==bank['name'])['instances'] if x['name']==cap['name'])
  self.assertAlmostEqual(cap2['parameters']['l']/base,1.1)
  p['overrides']={bank['name']+'/'+cap['name']:{'l':14e-6}}
  h,r=make_graph(p);cap3=next(x for x in next(y for y in h['subcircuits'] if y['name']==bank['name'])['instances'] if x['name']==cap['name']);self.assertEqual(cap3['parameters']['l'],14e-6)
 def test_startup_waveform_save_switch(self):
  from vco_studio.generator import emit_tb
  g,p=make_graph(default_project('class_b','tail'))
  no=emit_tb(g,p);self.assertIn('saveinit=no',no)
  p['simulation']['save_startup_waveform']=True
  yes=emit_tb(g,p);self.assertEqual(no.replace('saveinit=no','saveinit=yes'),yes)
  p['simulation']['save_startup_waveform']='false'
  with self.assertRaisesRegex(ValueError,'must be a boolean'):emit_tb(g,p)
 def test_invalid_binding_and_missing_mutual(self):
  g,p=make_graph(default_project('murphy','nmos'));b=copy.deepcopy(p['binding']);b['unresolved']=['cap'];self.assertFalse(validate_graph(g,b)['valid'])
  k=next(x for x in g['instances'] if x['role']=='mutual');k['parameters']['ind1']='absent';self.assertFalse(validate_graph(g,p['binding'])['valid'])

if __name__=='__main__':unittest.main()


class InitialConditionRules(unittest.TestCase):
    """HB start-up IC: centred on the actual bias and in the oscillating mode (Du multi-core, Guo gate at vdc)."""

    def test_ic_follow_bias_and_mode(self):
        import tempfile
        from vco_studio.catalog import default_project
        from vco_studio.generator import generate
        p = default_project('guo', 'wideband', 'demo_a')
        p['parameters']['vdc'] = 0.83
        d = tempfile.mkdtemp() + '/g'
        generate(p, d)
        ic = next(l for l in Path(d, 'tb.scs').read_text().splitlines() if l.startswith('ic '))
        self.assertIn('OUTP=0.8305', ic)
        p = default_project('du', 'autotracking', 'demo_a')
        d = tempfile.mkdtemp() + '/g'
        generate(p, d)
        ic = next(l for l in Path(d, 'tb.scs').read_text().splitlines() if l.startswith('ic '))
        self.assertIn('DUT.N2=0.5005', ic)
        self.assertIn('DUT.N1=0.4995', ic)
