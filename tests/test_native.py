"""Safety and electrical round-trip regressions; live receipts are separate."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from vco_studio.native import build_script, export, verify_netlist, prepare_workspace
from vco_studio.pdk import skill_tokens, _one_alias


def graph():
    return {'name':'probe','ports':[{'name':'P'},{'name':'N'}],'instances':[{'name':'R0','role':'res','ideal':True,'nodes':{'p':'P','n':'N'},'parameters':{'r':1000}}]}


class NativeTests(unittest.TestCase):
    def test_skill_symbols_and_pin_ambiguity(self):
        self.assertEqual(skill_tokens('(nil lr l wr w nf fingers)'),['nil','lr','l','wr','w','nf','fingers'])
        self.assertIsNone(_one_alias(['PLUS','A'],('p','PLUS','A')))

    def test_refuse_unknown_mapping_and_pdk_inductor(self):
        g=graph();g['instances'][0]['parameters']['typo']=1
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'Unmapped parameter'):
                build_script(g,{}, {'library':'Private','cell':'Probe'},d)
            with self.assertRaisesRegex(ValueError,'magnetic'):
                export(graph(),{'roles':{'ind':{'library':'pdk','cell':'spiral'}}},{'library':'Private','cell':'Probe'},d)

    def test_netlist_detects_silent_parameter_or_net_change(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'netlist'
            good='// Cell name: Probe\nR0 (P N) resistor r=1k\n'
            path.write_text(good)
            self.assertTrue(verify_netlist(path,graph(),{}, {'probe':'Probe'})['models_nodes_parameters_match'])
            path.write_text(good.replace('P N','P P'))
            with self.assertRaisesRegex(RuntimeError,'model/nets mismatch'):
                verify_netlist(path,graph(),{}, {'probe':'Probe'})
            path.write_text(good.replace('1k','2k'))
            with self.assertRaisesRegex(RuntimeError,'parameter differs'):
                verify_netlist(path,graph(),{}, {'probe':'Probe'})

    def test_hierarchy_cycle_is_rejected_before_write(self):
        g=graph();g['instances']=[{'name':'I0','role':'subckt','master':'probe','nodes':{},'parameters':{}}]
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'Recursive hierarchy'):
                build_script(g,{}, {'library':'Private','cell':'Probe'},d)

    def test_workspace_entry_is_private_and_refuses_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';source.mkdir();(source/'cds.lib').write_text('')
            library=root/'private';library.mkdir();(library/'cdsinfo.tag').write_text('')
            receipt={'status':'verified','target':{'workspace':str(source),'library':'Private','cell':'Top','library_path':str(library)}}
            profile={'virtuoso':'/existing/virtuoso'}
            result=prepare_workspace(receipt,profile,root/'entry')
            self.assertEqual(result['mode'],'read-only')
            self.assertIn('DEFINE Private',Path(result['cds_lib']).read_text())
            self.assertNotIn('-nograph',(root/'entry/open-virtuoso.sh').read_text())
            (root/'entry/cds.lib').write_text('user changes')
            with self.assertRaises(FileExistsError):
                prepare_workspace(receipt,profile,root/'entry')
            receipt['status']='failed'
            with self.assertRaisesRegex(ValueError,'verified'):
                prepare_workspace(receipt,profile,root/'another')

    def test_existing_target_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            existing=Path(d)/'library';existing.mkdir()
            with self.assertRaises(FileExistsError):
                export(graph(),{}, {'library':'Private','cell':'Probe','library_path':str(existing)},Path(d)/'export')

if __name__=='__main__':unittest.main()
