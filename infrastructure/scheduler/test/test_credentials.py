import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
import run_with_credentials as module

ID = 'a1234567-1234-4234-8234-123456789abc'
VALUES = dict(line.split('=',1) for line in (ROOT/'.dev.vars.example').read_text(encoding='utf-8').splitlines() if line and not line.startswith('#'))
CONFIG = {key:VALUES[key].strip('"') for key in module.validate_snapshot.__globals__['BOT_KEYS']}
RAW = json.dumps(CONFIG,ensure_ascii=False,separators=(',',':'))
CREDS = {'OSEP_USER':'user-placeholder','OSEP_PASS':'pass-placeholder'}

class CredentialsTests(unittest.TestCase):
    def environment(self):
        return {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'fzucarello/Turnos','GITHUB_REF':'refs/heads/main','REQUEST_ID':ID,'FORCE_DRY_RUN':'true','ACTIONS_ID_TOKEN_REQUEST_URL':'https://pipelines.actions.githubusercontent.com/token?api-version=2','ACTIONS_ID_TOKEN_REQUEST_TOKEN':'technical-placeholder'}

    def runtime(self):
        return {'credentials':CREDS,'config_json':RAW,'config_sha256':hashlib.sha256(RAW.encode()).hexdigest()}

    def test_configuration_and_credentials_stay_in_memory(self):
        calls=[]
        def request(url,token,data=None):
            calls.append((url,token,data))
            return {'value':'signed-placeholder'} if data is None else self.runtime()
        creds,config,token=module.obtain_credentials(self.environment(),request)
        self.assertEqual(creds,CREDS)
        self.assertEqual(config['DRY_RUN'],'true')
        self.assertEqual(json.loads(calls[1][2]),{'request_id':ID})
        self.assertNotIn(CONFIG['OBJ_MEDICO'],str(calls))
        self.assertIn('audience=https%3A%2F%2Fosep-scheduler.franchezuca.workers.dev%2Fcredentials',calls[0][0])

    def test_untrusted_environment_or_identity_url_never_sends(self):
        for key,value in [('GITHUB_ACTIONS','false'),('GITHUB_REPOSITORY','other/repo'),('GITHUB_REF','refs/heads/other'),('REQUEST_ID','invalid'),('ACTIONS_ID_TOKEN_REQUEST_URL','https://attacker.example/token')]:
            env=self.environment();env[key]=value
            with self.assertRaises(ValueError):module.obtain_credentials(env,lambda *args:self.fail('unexpected HTTP'))

    def test_invalid_payloads_fail_closed(self):
        for result in [{}, {**self.runtime(),'config_sha256':'a'*64}, {**self.runtime(),'credentials':{'OSEP_USER':'u','OSEP_PASS':''}}, {**self.runtime(),'extra':'x'}]:
            with self.assertRaises(ValueError):module.obtain_credentials(self.environment(),lambda url,token,data=None:{'value':'signed-placeholder'} if data is None else result)

    def test_runtime_failure_prints_no_private_exception_and_does_not_start(self):
        output=io.StringIO()
        with patch.object(module,'obtain_credentials',side_effect=RuntimeError('PRIVATE_DIAGNOSTIC')),patch.object(module,'run_private_bot') as run,contextlib.redirect_stdout(output):
            self.assertEqual(module.main(),4);run.assert_not_called()
        self.assertNotIn('PRIVATE_DIAGNOSTIC',output.getvalue())

    def test_child_cannot_write_actions_summary_outputs_or_access_identity(self):
        env={**self.environment(),'GITHUB_ENV':'private-env-file','GITHUB_STEP_SUMMARY':'private-summary-file','GITHUB_OUTPUT':'output-file','GITHUB_TOKEN':'token-placeholder','ACTIONS_RUNTIME_TOKEN':'runtime-placeholder'}
        child=module.private_child_environment(env,CONFIG,CREDS)
        self.assertEqual(child['OSEP_PASS'],CREDS['OSEP_PASS'])
        for key in ['GITHUB_ENV','GITHUB_STEP_SUMMARY','GITHUB_OUTPUT','GITHUB_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN','ACTIONS_RUNTIME_TOKEN']:self.assertNotIn(key,child)

    def test_bot_confirmation_and_errors_never_reach_public_stdout_or_summary(self):
        for code in [0,1,3,6]:
            output=io.StringIO()
            with tempfile.TemporaryDirectory() as tmp:
                summary=pathlib.Path(tmp)/'summary'
                with patch.dict(module.os.environ,{**self.environment(),'GITHUB_STEP_SUMMARY':str(summary)},clear=True),patch.object(module,'obtain_credentials',return_value=(CREDS,CONFIG,'signed-placeholder')),patch.object(module,'run_private_bot',return_value=(code,'PRIVATE_PATIENT ::error::PRIVATE_CONFIRMATION',False)),patch.object(module,'save_result') as save,contextlib.redirect_stdout(output):
                    self.assertEqual(module.main(),code)
                    self.assertIn('PRIVATE_PATIENT',save.call_args.args[2])
                self.assertNotIn('PRIVATE_PATIENT',output.getvalue()+summary.read_text())
                self.assertNotIn('PRIVATE_CONFIRMATION',output.getvalue())

    def test_result_failure_does_not_fallback_to_public_log(self):
        output=io.StringIO()
        with patch.object(module,'obtain_credentials',return_value=(CREDS,CONFIG,'signed-placeholder')),patch.object(module,'run_private_bot',return_value=(1,'PRIVATE_CONFIRMATION',False)),patch.object(module,'save_result',side_effect=RuntimeError('PRIVATE_ERROR')),contextlib.redirect_stdout(output):
            self.assertEqual(module.main(),4)
        self.assertNotIn('PRIVATE_CONFIRMATION',output.getvalue());self.assertNotIn('PRIVATE_ERROR',output.getvalue())

    def test_private_log_is_bounded_and_secrets_are_removed_before_upload(self):
        sent=[]
        def request(url,token,data=None):
            if data is None:return {'value':'fresh-token-placeholder'}
            sent.append(json.loads(data));return {'ok':True}
        with contextlib.redirect_stdout(io.StringIO()):module.save_result(self.environment(),3,'x'*40000+' pass-placeholder user-placeholder',False,CONFIG,list(CREDS.values()),request)
        self.assertLessEqual(len(sent[0]['log'].encode()),module.LOG_LIMIT)
        self.assertTrue(sent[0]['log_truncated'])
        self.assertNotIn('pass-placeholder',sent[0]['log'])
        self.assertEqual(sent[0]['status'],'error_bot')

    def test_capture_real_subprocess_stderr_and_workflow_commands(self):
        # Ejecutable de muestra, sin portal ni credenciales reales.
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp,'app.py').write_text("import sys; print('PRIVATE_PATIENT'); print('::error::PRIVATE_ERROR',file=sys.stderr)")
            with patch.object(module.os,'getcwd',return_value=tmp):
                original=module.subprocess.Popen
                with patch.object(module.subprocess,'Popen',side_effect=lambda *a,**kw:original(*a,cwd=tmp,**kw)),contextlib.redirect_stdout(io.StringIO()) as output:
                    code,log,truncated=module.run_private_bot({})
            self.assertEqual(code,0);self.assertIn('PRIVATE_PATIENT',log);self.assertIn('PRIVATE_ERROR',log);self.assertEqual(output.getvalue(),'')

    def test_preflight_never_starts_or_saves_result(self):
        with patch.object(module,'obtain_credentials',return_value=(CREDS,CONFIG,'signed-placeholder')),patch.object(module,'run_private_bot') as run,patch.object(module,'save_result') as save,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(check_only=True),0);run.assert_not_called();save.assert_not_called()

    def test_redirects_and_sensitive_http_details_are_suppressed(self):
        self.assertIsNone(module.NoRedirect().redirect_request(None,None,302,'',{},'https://attacker.example'))
        error=module.urllib.error.HTTPError('https://private.example/token',401,'PRIVATE_ERROR',{},None)
        with self.assertRaisesRegex(ValueError,'identity_http_401'):module.stage_request(lambda *a:(_ for _ in ()).throw(error),'identity','https://allowed.example','token')

if __name__=='__main__':unittest.main()
