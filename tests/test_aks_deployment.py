"""Offline AKS manifest, explicit mode, persistent no-replay and binding checks."""
import copy,importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from qa.config import ROOT

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
entry=module('aks_entrypoint',ROOT/'deploy/aks/entrypoint.py')
renderer=module('aks_renderer',ROOT/'deploy/aks/render_job.py')

class AksDeploymentTests(unittest.TestCase):
    def config(self):
        c=json.loads((ROOT/'config/settings.json').read_text());c['response_mapping_confirmed']=True
        e={'QA_MSISDN_A':'SYNTHETIC_ONLY','MORPHEUS_API_KEY':'SYNTHETIC_KEY'}
        return c,e
    def test_rendered_job_is_serial_no_restart_nonroot_and_dry_by_default(self):
        j=renderer.render('synthetic.azurecr.io/qa@sha256:'+'a'*64,'synthetic-run')
        self.assertEqual(j['kind'],'Job');self.assertEqual(j['spec']['backoffLimit'],0);self.assertEqual(j['spec']['parallelism'],1)
        p=j['spec']['template']['spec'];self.assertEqual(p['restartPolicy'],'Never');self.assertFalse(p['automountServiceAccountToken'])
        c=p['containers'][0];self.assertEqual(c['args'][3],'plan');self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
        self.assertTrue(any(v['mountPath']=='/app/reports' for v in c['volumeMounts']))
    def test_live_requires_secret_and_safety_keeps_fixed_caps(self):
        j=renderer.render('synthetic.azurecr.io/qa@sha256:'+'a'*64,'synthetic-run','safety','live')
        c=j['spec']['template']['spec']['containers'][0]
        self.assertFalse(c['envFrom'][0]['secretRef']['optional']);self.assertIn('--allow-draft-references',c['args']);self.assertIn('510',c['args'])
        with self.assertRaises(ValueError):renderer.render('synthetic.azurecr.io/qa:latest','run')
        with self.assertRaises(ValueError):renderer.render('synthetic.azurecr.io/qa@sha256:'+'a'*64,'run','core','resume')
    def test_core_scope_excludes_writes_and_isolation(self):
        self.assertEqual(entry.selection('BASELINE-001')[0]['id'],'BASELINE-001')
        for case in ('SAFE-PII-005','TXN-001','BASELINE-001,BASELINE-001'):
            with self.assertRaises(ValueError):entry.selection(case)
    def test_profile_is_seeded_once_and_drift_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'deploy/aks';p.mkdir(parents=True);(p/'evaluator-profile.json').write_text('{}')
            dest=entry.bootstrap_profile(root);self.assertEqual(dest.read_text(),'{}')
            (p/'evaluator-profile.json').write_text('{"changed":true}')
            with self.assertRaises(ValueError):entry.bootstrap_profile(root)
            self.assertEqual(dest.read_text(),'{}')
    def test_live_reservation_blocks_replay_even_after_inconclusive_exit(self):
        c,e=self.config()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'reports').mkdir();(root/'deploy/aks').mkdir(parents=True);(root/'deploy/aks/evaluator-profile.json').write_text('{}')
            with patch.object(entry,'ROOT',root),patch.object(entry,'load',return_value=(c,e)),patch.object(entry,'bootstrap_profile'),patch.object(entry,'dispatch',return_value=2) as dispatch:
                self.assertEqual(entry.main(['--run-id','synthetic-run','--mode','live']),2)
                with self.assertRaises(ValueError):entry.main(['--run-id','synthetic-run','--mode','live'])
                self.assertEqual(dispatch.call_count,1)
    def test_plan_then_live_and_binding_drift_refusal(self):
        c,e=self.config()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'reports').mkdir();(root/'deploy/aks').mkdir(parents=True);(root/'deploy/aks/evaluator-profile.json').write_text('{}')
            with patch.object(entry,'ROOT',root),patch.object(entry,'load',return_value=(c,e)),patch.object(entry,'bootstrap_profile'),patch.object(entry,'dispatch',return_value=0):
                self.assertEqual(entry.main(['--run-id','synthetic-run']),0)
                self.assertFalse((root/'reports/aks/synthetic-run/live_reservation.json').exists())
                self.assertEqual(entry.main(['--run-id','synthetic-run','--mode','live']),0)
                e['MORPHEUS_API_KEY']='SYNTHETIC_CHANGED'
                with self.assertRaisesRegex(ValueError,'Run inputs'):entry.main(['--run-id','synthetic-run','--mode','plan'])
    def test_no_private_config_copy_or_mac_venv_in_container_recipe(self):
        text=(ROOT/'Dockerfile').read_text()
        self.assertNotIn('COPY . .',text);self.assertNotIn('config/local.json',text);self.assertNotIn('COPY reports',text)
        self.assertIn('USER 10001:10001',text)
