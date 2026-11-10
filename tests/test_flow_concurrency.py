"""Real spawn processes and simulator subprocesses; no optimizer/flow mocks.

The configured solver is an explicit slow failure executable, not Spectre.
It never generates electrical measurements. The test exercises the real
shared generator, simulator.run receipts, flow state writes and OS flock.
"""
import datetime
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

INSTALL=Path(__file__).resolve().parents[1]
if str(INSTALL) not in sys.path:sys.path.insert(0,str(INSTALL))
from vco_studio.common import read_json,write_json


def load_flow():
    staged=Path(__file__).with_name('flow.py')
    path=Path(os.environ.get('VCO_FLOW_UNDER_TEST',str(staged if staged.is_file() else INSTALL/'vco_studio/flow.py')))
    spec=importlib.util.spec_from_file_location('vco_studio._flow_concurrency_target',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def worker(operation,workspace,project,ready,reply,gate=None):
    flow=load_flow()
    ready.set()
    if gate is not None:gate.wait(10)
    try:
        if operation=='add':result=flow.add_candidate(workspace,project)
        elif operation=='measure':result=flow.measure(workspace,'candidate_001',timeout_s=15)
        elif operation=='sweep':result=flow.sweep(workspace,'candidate_001',[{'main':2}],timeout_s=15)
        elif operation=='bad-add':result=flow.add_candidate(workspace,dict(project,parameters={'vdd':.8}))
        else:raise AssertionError(operation)
        reply.put({'operation':operation,'status':'ok','result':result})
    except Exception as exc:reply.put({'operation':operation,'status':'error','type':type(exc).__name__,'error':str(exc)})


class ConcurrentFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.context=multiprocessing.get_context('spawn');self.processes=[]
        self.control=self.root/'control';self.control.mkdir()
        self.solver=self.root/'intentional-slow-failure'
        self.solver.write_text('#!'+sys.executable+'\n'+f'''import os,time
from pathlib import Path
control=Path({str(self.control)!r})
fd=os.open(str(control/'started'),os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
os.write(fd,(str(os.getpid())+'\\n').encode());os.close(fd)
deadline=time.monotonic()+12
while not (control/'release').exists() and time.monotonic()<deadline:time.sleep(.02)
raise SystemExit(7)
''')
        self.solver.chmod(0o700)
        self.project={'family':'murphy','variant':'nmos','tools':{'spectre':str(self.solver),'native_env':str(self.root/'no-environment-script')}}
        self.workspace=self.root/'task';self.create_state(self.workspace)
        self.flow=load_flow();self.flow.add_candidate(self.workspace,self.project)

    def create_state(self,workspace,budget=10):
        workspace.mkdir()
        state={'schema':'vco.task.v1','created':datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'spec':{'frequency_band_hz':[9e9,12e9],'power_max_mw':10,'vdd':1.0,'main_bits':4},
               'candidates':[],'events':[],'stage':'selection','budget':{'solver_processes':budget},'completed':False,'evidence':[]}
        write_json(workspace/'task.json',state)

    def tearDown(self):
        (self.control/'release').touch()
        for process in self.processes:
            process.join(5)
            if process.is_alive():process.terminate();process.join(3)
        self.temp.cleanup()

    def launch(self,operation,workspace=None,gate=None):
        ready=self.context.Event();reply=self.context.Queue()
        process=self.context.Process(target=worker,args=(operation,str(workspace or self.workspace),self.project,ready,reply,gate))
        self.processes.append(process);process.start()
        self.assertTrue(ready.wait(10),'worker failed to initialize')
        return process,reply

    def wait_solver_started(self,count=1):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            path=self.control/'started'
            if path.exists() and len(path.read_text().splitlines())>=count:return
            time.sleep(.02)
        self.fail('real simulator subprocess did not start')

    def finish(self,process,reply):
        process.join(10)
        self.assertFalse(process.is_alive(),'transaction did not complete')
        self.assertEqual(process.exitcode,0)
        return reply.get(timeout=2)

    def assert_blocked(self,process):
        process.join(.35)
        self.assertTrue(process.is_alive(),'same-task mutation returned while solver owned transaction')

    def test_measure_and_add_preserve_both_results_and_candidate_registration(self):
        measured,mreply=self.launch('measure');self.wait_solver_started()
        added,areply=self.launch('add');self.assert_blocked(added)
        # Read-only status must not wait for the long mutation lock.
        started=time.monotonic();snapshot=self.flow.status(self.workspace)
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(len(snapshot['candidates']),1)
        (self.control/'release').touch()
        self.assertEqual(self.finish(measured,mreply)['status'],'ok')
        self.assertEqual(self.finish(added,areply)['status'],'ok')
        state=read_json(self.workspace/'task.json')
        self.assertEqual([row['id'] for row in state['candidates']],['candidate_001','candidate_002'])
        self.assertEqual(len(state['candidates'][0]['results']),1)
        self.assertEqual(read_json(state['candidates'][0]['results'][0])['receipt']['exit_code'],7)
        self.assertEqual(self.flow.budget_usage(state)['solver_processes'],1)

    def test_sweep_and_add_preserve_both_mutations(self):
        sweeping,sreply=self.launch('sweep');self.wait_solver_started()
        added,areply=self.launch('add');self.assert_blocked(added)
        (self.control/'release').touch()
        self.assertEqual(self.finish(sweeping,sreply)['status'],'ok')
        self.assertEqual(self.finish(added,areply)['status'],'ok')
        state=read_json(self.workspace/'task.json')
        self.assertEqual(len(state['candidates']),2)
        self.assertEqual(len(state['candidates'][0]['results']),1)
        self.assertEqual([e['action'] for e in state['events']][-2:],['code_point','add_candidate'])

    def test_concurrent_measures_cannot_overspend_last_solver_slot(self):
        state=read_json(self.workspace/'task.json');state['budget']['solver_processes']=1;write_json(self.workspace/'task.json',state)
        first,freply=self.launch('measure');self.wait_solver_started()
        second,sreply=self.launch('measure');self.assert_blocked(second)
        (self.control/'release').touch()
        self.assertEqual(self.finish(first,freply)['status'],'ok')
        denied=self.finish(second,sreply)
        self.assertEqual(denied['status'],'error');self.assertIn('solver budget exhausted',denied['error'])
        self.assertEqual(len((self.control/'started').read_text().splitlines()),1)
        state=read_json(self.workspace/'task.json')
        self.assertEqual(len(state['candidates'][0]['results']),1)
        self.assertEqual(self.flow.budget_usage(state)['solver_processes'],1)

    def test_concurrent_sweeps_allocate_distinct_points_and_keep_results(self):
        first,freply=self.launch('sweep');self.wait_solver_started()
        second,sreply=self.launch('sweep');self.assert_blocked(second)
        (self.control/'release').touch()
        self.assertEqual(self.finish(first,freply)['status'],'ok')
        self.assertEqual(self.finish(second,sreply)['status'],'ok')
        state=read_json(self.workspace/'task.json');candidate=state['candidates'][0]
        self.assertEqual(len(candidate['results']),2);self.assertEqual(len(set(candidate['results'])),2)
        self.assertEqual(sorted(p.name for p in (Path(candidate['directory'])/'code_grid').iterdir()),['point_0000','point_0001'])
        self.assertEqual(self.flow.budget_usage(state)['solver_processes'],2)

    def test_parallel_adds_preserve_orphan_and_allocate_unique_candidates(self):
        orphan=self.workspace/'candidates/candidate_002';orphan.mkdir();(orphan/'KEEP').write_text('historical orphan evidence')
        gate=self.context.Event();a,aq=self.launch('add',gate=gate);b,bq=self.launch('add',gate=gate);gate.set()
        self.assertEqual(self.finish(a,aq)['status'],'ok');self.assertEqual(self.finish(b,bq)['status'],'ok')
        state=read_json(self.workspace/'task.json')
        self.assertEqual([row['id'] for row in state['candidates']],['candidate_001','candidate_003','candidate_004'])
        self.assertEqual((orphan/'KEEP').read_text(),'historical orphan evidence')

    def test_other_task_can_mutate_while_this_task_runs_solver(self):
        other=self.root/'other-task';self.create_state(other)
        first,freply=self.launch('measure');self.wait_solver_started()
        added,areply=self.launch('add',workspace=other)
        self.assertEqual(self.finish(added,areply)['status'],'ok')
        self.assertTrue(first.is_alive())
        (self.control/'release').touch();self.assertEqual(self.finish(first,freply)['status'],'ok')

    def test_failed_mutation_releases_lock(self):
        bad,bq=self.launch('bad-add');self.assertEqual(self.finish(bad,bq)['status'],'error')
        good,gq=self.launch('add');self.assertEqual(self.finish(good,gq)['status'],'ok')
        self.assertEqual(len(read_json(self.workspace/'task.json')['candidates']),2)


if __name__=='__main__':unittest.main(verbosity=2)
