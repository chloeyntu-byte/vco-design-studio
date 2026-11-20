import copy,tempfile,unittest
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path
from synthetic_topology import use_two_banks
from vco_studio.catalog import get_variant,list_families,default_project
from vco_studio.generator import make_graph,validate_graph,generate
from vco_studio.characterize import bank_ac

class GeneratorTests(unittest.TestCase):
 def test_all_variants_and_main_widths(self):
  for fam in list_families():
   for v in fam['variants']:
    p=default_project(fam['id'],v['id']);p['registers']['main']={'bits':6,'value':43}
    g,p=make_graph(p);self.assertTrue(validate_graph(g,p['binding'])['valid'])
    self.assertEqual(sum(x['name'].startswith('main_B') for x in g['ports']),6)
    bank=next(x for x in g['subcircuits'] if x['name']=='CT_R3_main');self.assertEqual(len(bank['instances']),63)
    self.assertEqual(set(p['registers']), {'main'})
 def test_independent_register_widths_on_synthetic_graph(self):
  with ExitStack() as stack:
   use_two_banks(stack)
   p=default_project('class_b','tail')
   p['registers'].update(main={'bits':6,'value':43}, aux={'bits':3,'value':5})
   graph,project=make_graph(p)
  self.assertTrue(validate_graph(graph,project['binding'])['valid'])
  for register,bits in (('main',6),('aux',3)):
   self.assertEqual(sum(port['name'].startswith(register+'_B') for port in graph['ports']),bits)
   bank=next(cell for cell in graph['subcircuits'] if cell['name']=='CT_R3_'+register)
   self.assertEqual(len(bank['instances']),2**bits-1)
 def test_fixed_cap_and_invalid_register_rejected(self):
  p=default_project('class_b','nmos')
  variant=get_variant('class_b','nmos')
  graph=variant['graph']
  inst=next(x for x in graph['instances'] if x['role']=='cap')
  inst['parameters']['c']=50e-15
  p['overrides']={graph['name']+'/'+inst['name']:{'c':60e-15}}
  with patch('vco_studio.generator.get_variant',return_value=variant):
   with self.assertRaisesRegex(ValueError,'50fF'):make_graph(p)
  p=default_project('class_b');p['registers']['aux']={'bits':4,'value':0}
  with self.assertRaisesRegex(ValueError,'no independent'):make_graph(p)
 def test_physical_identity_codes_settings_and_geometry(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);p=default_project('class_b','nmos');a=generate(p,root/'a')
   q=copy.deepcopy(p);q['registers']['main']['value']=5;q['parameters']['frequency_guess_ghz']=11;q['simulation']['harmonics']=21
   b=generate(q,root/'b');self.assertEqual(a['physical_identity'],b['physical_identity'])
   q['parameters']['vdd']=.9;c=generate(q,root/'c');self.assertNotEqual(a['physical_identity'],c['physical_identity'])
   (root/'a'/'circuit.scs').write_text('modified')
   with self.assertRaisesRegex(ValueError,'changed after manifest'):bank_ac(root/'a',root/'bad')
 def test_target_seed_then_scale_and_override(self):
  p=default_project('class_b','nmos',str(Path(__file__).resolve().parents[1]/'examples/bindings/demo_b.json'))
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
  g,p=make_graph(default_project('class_b','tail'));b=copy.deepcopy(p['binding']);b['unresolved']=['cap'];self.assertFalse(validate_graph(g,b)['valid'])
  k=next(x for x in g['instances'] if x['role']=='mutual');k['parameters']['ind1']='absent';self.assertFalse(validate_graph(g,p['binding'])['valid'])

if __name__=='__main__':unittest.main()


class InitialConditionRules(unittest.TestCase):
    def test_default_startup_kick_follows_tank_supply(self):
        with tempfile.TemporaryDirectory() as folder:
            for variant in ('nmos', 'tail'):
                p = default_project('class_b', variant, 'demo_a')
                p['parameters'].update(vdd=0.83, vdc=0.4)
                dest = Path(folder) / variant
                generate(p, dest)
                ic = next(line for line in (dest / 'tb.scs').read_text().splitlines() if line.startswith('ic '))
                self.assertEqual(ic, 'ic OUTP=0.8305 OUTN=0.8295')

    def test_explicit_initial_conditions_override_default(self):
        from vco_studio.generator import emit_tb
        project = default_project('class_b', 'tail')
        project['simulation']['initial_conditions'] = {'OUTP': 0.9, 'I22.S': 0.2}
        graph, project = make_graph(project)
        self.assertIn('ic OUTP=0.9 DUT.S=0.2', emit_tb(graph, project).splitlines())
