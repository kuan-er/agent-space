import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from helpers import ROOT
from taskd.checkers.base import CheckContext
from taskd.checkers.eps_preflight import EpsPreflightChecker

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.ctx=CheckContext(self.root,time.time());self.checker=EpsPreflightChecker()
        self.program=b'<Xpl-source><VarDeclarations><VarExtern><Var><Name>ipaint_gun_index</Name></Var></VarExtern></VarDeclarations><Pous><Pou><Body><dest>ipaint_gun_index</dest></Body></Pou></Pous></Xpl-source>'
        (self.root/'module.dll').write_bytes(b'test fixture module');(self.root/'program.XPL').write_bytes(self.program)
        self.write('config.json',{'AllowedProjectFolder':'C:\\work\\current'})
        self.write('snapshot.json',{'utc':self.now(),'eps_pid':21,'project_folder':'C:\\work\\current','host_is_ready':True,'read_errors':{}})
        self.write('parser.json',{'utc':self.now(),'eps_pid':21,'source':'eps_native_parser','program_sha256':hashlib.sha256(self.program).hexdigest(),'success':True,'errors':[]})
        self.p=self.checker.validate(dict(config='config.json',dll='module.dll',dll_sha256=hashlib.sha256(b'test fixture module').hexdigest(),project_folder='C:\\work\\current',program='program.XPL',snapshot='snapshot.json',parser_result='parser.json'))
    def now(self):return datetime.now(timezone.utc).isoformat()
    def write(self,name,value):(self.root/name).write_text(json.dumps(value))
    def result(self):return self.checker.check(self.p,self.ctx)
    def test_matching_fixture_is_only_file_preflight(self):
        result=self.result();self.assertEqual(result.outcome,'pass');self.assertEqual(result.layer,'file')
        self.assertFalse(result.observed['native_deposition_verified'])
    def test_old_project_reports_expected_and_actual(self):
        self.write('config.json',{'AllowedProjectFolder':'C:\\work\\old'})
        result=self.result();self.assertEqual(result.outcome,'fail');self.assertEqual(result.observed['code'],'configuration_mismatch')
        self.assertEqual(result.observed['issues'][0]['actual'],'C:\\work\\old')
    def test_missing_external_declaration_is_program_error(self):
        (self.root/'program.XPL').write_bytes(b'<Xpl-source><Pous><Pou><Body><dest>ipaint_gun_index</dest></Body></Pou></Pous></Xpl-source>')
        self.assertIn('external_variables',[i['field'] for i in self.result().observed['issues']])
    def test_stale_or_wrong_program_receipt_never_passes(self):
        self.write('parser.json',{'utc':'2000-01-01T00:00:00Z','eps_pid':21,'source':'eps_native_parser','program_sha256':'0'*64,'success':True,'errors':[]})
        fields=[i['field'] for i in self.result().observed['issues']]
        self.assertIn('parser_result.utc',fields);self.assertIn('parser_program_sha256',fields)
    def test_missing_native_evidence_never_passes(self):
        (self.root/'parser.json').unlink();self.assertEqual(self.result().outcome,'fail')
    def test_native_compile_failure_stays_distinct(self):
        data=json.loads((self.root/'parser.json').read_text());data.update(success=False,errors=['ThicknessMap compile failed']);self.write('parser.json',data)
        self.assertEqual(self.result().observed['code'],'compile_failed')
