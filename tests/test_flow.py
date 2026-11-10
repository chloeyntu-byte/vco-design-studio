"""Workflow contract tests. No solver is invoked or simulated as a real run."""
import copy
import os
import datetime
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from vco_studio import flow
from vco_studio.catalog import default_project
from vco_studio.common import read_json,write_json,sha256


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.spec={'frequency_band_hz':[10e9,12e9],'power_max_mw':10,'vdd':1.0,'main_bits':4}
        self.state={'schema':'vco.task.v1','created':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'spec':self.spec,'candidates':[],'events':[],'stage':'selection','budget':{},'completed':False,'evidence':[]}
        write_json(self.root/'task.json',self.state)

    def tearDown(self):self.temp.cleanup()

    def test_add_candidate_rejects_explicit_requirements_conflict(self):
        for parameters,registers in [({'vdd':.9},{}),({}, {'main':{'bits':5}})]:
            with self.assertRaisesRegex(ValueError,'conflicts'):
                flow.add_candidate(self.root,{'family':'murphy','variant':'nmos','parameters':parameters,'registers':registers})
        self.assertEqual(read_json(self.root/'task.json')['candidates'],[])
        self.assertFalse((self.root/'candidates').exists())

    def test_add_candidate_inherits_task_requirements_before_real_generation(self):
        self.state['spec'].update(vdd=.9,main_bits=3);write_json(self.root/'task.json',self.state)
        row=flow.add_candidate(self.root,{'family':'murphy','variant':'nmos'})
        project=read_json(Path(row['directory'])/'project.json')
        self.assertEqual(project['parameters']['vdd'],.9)
        self.assertEqual(project['registers']['main']['bits'],3)
        self.assertLess(project['registers']['main']['value'],8)
        self.assertEqual(set(row['inherited_task_requirements']),{'vdd','main_bits'})

    def test_budget_counts_solver_receipts_not_tool_summaries_and_deduplicates(self):
        # Metadata-only unit fixtures, never presented as simulation evidence.
        candidate=self.root/'candidate';candidate.mkdir()
        hb=candidate/'hb';hb.mkdir()
        receipt={'schema':'vco.run.v1','pid':123,'start_epoch':1,'cwd':str(hb)}
        write_json(hb/'receipt.json',receipt)
        duplicate=candidate/'copied-receipt';duplicate.mkdir();write_json(duplicate/'receipt.json',receipt)
        tool=candidate/'optimizer';tool.mkdir();write_json(tool/'receipt.json',{'schema':'vco.tool-receipt.v1','tool':'optimizer'})
        ac=candidate/'ac';ac.mkdir();write_json(ac/'receipt.json',{'run_dir':str(ac),'argv':['spectre'],'exit_code':1,'input_sha256':'fixture','excitation':0})
        self.state['candidates']=[{'directory':str(candidate),'results':[str(hb/'result.json')]}]
        self.state['budget']={'solver_processes':2}
        self.assertEqual(flow.budget_usage(self.state)['solver_processes'],2)
        with self.assertRaisesRegex(ValueError,'solver budget'):flow.check_budget(self.state)
        self.state['budget']['solver_processes']=3
        self.assertEqual(flow.check_budget(self.state)['solver_processes'],2)

    @staticmethod
    def comparison_rows():
        # Pure comparison-function inputs; not result.json or claimed solver runs.
        base={'code':'{"main":{"bits":4,"value":1}}','file':'baseline',
              'settings':{'hb.maxharms':15.0,'opts.reltol':1e-4},
              'result':{'metrics':{'frequency_hz':10e9,'power_mw':5.,'pn_1000000_dbc_hz':-110.}}}
        other=copy.deepcopy(base);other['file']='independent';other['settings']['hb.maxharms']=25.
        other['result']['metrics'].update(frequency_hz=10.005e9,power_mw=5.02,pn_1000000_dbc_hz=-109.8)
        return base,other

    def test_convergence_checks_code_actual_settings_and_each_error_limit(self):
        base,other=self.comparison_rows()
        compared=flow._compare_convergence(base,other,flow.CONVERGENCE_DEFAULTS,{})
        self.assertAlmostEqual(compared['errors']['frequency_rel'],.0005)
        for field,value,pattern in [('frequency_hz',10.02e9,'frequency_rel'),('power_mw',5.1,'power_rel'),('pn_1000000_dbc_hz',-109.,'pn_abs_db')]:
            bad=copy.deepcopy(other);bad['result']['metrics'][field]=value
            with self.assertRaisesRegex(ValueError,pattern):flow._compare_convergence(base,bad,flow.CONVERGENCE_DEFAULTS,{})
        other['settings']=copy.deepcopy(base['settings'])
        with self.assertRaisesRegex(ValueError,'different actual'):flow._compare_convergence(base,other,flow.CONVERGENCE_DEFAULTS,{})
        other['code']='different-code'
        with self.assertRaisesRegex(ValueError,'matching adopted'):flow._compare_convergence(base,other,flow.CONVERGENCE_DEFAULTS,{})

    def test_solver_setting_identity_ignores_comments_and_control_codes(self):
        first='parameters MAIN_CODE=1\nhb (OUTP OUTN) hb tstab=60n maxharms=[15] errpreset=conservative\nopts options temp=55 reltol=1e-4\n'
        second=first.replace('MAIN_CODE=1','MAIN_CODE=2').replace('1e-4','0.0001')+'// changed prose\n'
        self.assertEqual(flow._solver_settings(first),flow._solver_settings(second))
        self.assertNotEqual(flow._solver_settings(first),flow._solver_settings(first.replace('[15]','[25]')))

    def test_finite_metrics_are_required(self):
        self.assertIn('invalid_frequency_hz',flow.assess_metrics({'frequency_hz':float('nan'),'power_mw':5},self.spec))
        self.assertIn('invalid_power_mw',flow.assess_metrics({'frequency_hz':1e10,'power_mw':float('inf')},self.spec))


class ExistingRealEvidenceTests(unittest.TestCase):
    """Receipt integrity regressions using explicitly synthetic replay, never measured evidence."""
    def setUp(self):
        from replay_fixture import create_result
        self.temp = tempfile.TemporaryDirectory()
        self.path = create_result(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_real_receipt_and_raw_psf_metrics_validate(self):
        if not self.path.is_file():self.skipTest('Local real Spectre evidence not present')
        result=read_json(self.path);generated=Path(result['receipt']['generated_dir']);manifest=read_json(generated/'manifest.json')
        verified=flow._verify_result(self.path,manifest['physical_identity'],sha256(generated/'circuit.scs'))
        self.assertEqual(verified['result']['status'],'measured')
        self.assertIn('hb.maxharms',verified['settings'])

    def test_modified_raw_evidence_is_rejected_in_private_copy(self):
        if not self.path.is_file():self.skipTest('Local real Spectre evidence not present')
        # Copy synthetic replay files, then deliberately corrupt one byte.
        # This tests detection only; it creates no new measured result claim.
        with tempfile.TemporaryDirectory() as temporary:
            dest=Path(temporary)/'corrupted-evidence';shutil.copytree(self.path.parent,dest)
            result=read_json(dest/'result.json');receipt=read_json(dest/'receipt.json')
            result['path']=str(dest);receipt['cwd']=str(dest);result['receipt']=receipt
            write_json(dest/'result.json',result);write_json(dest/'receipt.json',receipt)
            raw=dest/'psf/hb.fd.pss_hb';raw.write_bytes(raw.read_bytes()+b'\n// deliberate integrity-test corruption\n')
            with self.assertRaisesRegex(ValueError,'Raw evidence hash changed'):
                flow._verify_result(dest/'result.json',result['physical_identity'],sha256(dest/'circuit.scs'))


if __name__=='__main__':unittest.main()
