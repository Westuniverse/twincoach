import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('stage_runtime',ROOT/'build/stage.py')
staging=importlib.util.module_from_spec(spec);spec.loader.exec_module(staging)

class BuildTests(unittest.TestCase):
    def test_runtime_mode_selects_its_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            full=staging.stage(Path(directory)/'full',mode='full33')
            hybrid=staging.stage(Path(directory)/'hybrid',mode='hybrid')
        full_roles={x['role'] for x in full['missing_required_assets']}
        hybrid_roles={x['role'] for x in hybrid['missing_required_assets']}
        self.assertIn('prefix_xmodel',full_roles)
        self.assertIn('prefix_xmodel',hybrid_roles)
        self.assertNotIn('lightning_xmodel',full_roles)
        self.assertNotIn('hybrid_pair_worker',full_roles)
        self.assertIn('lightning_xmodel',hybrid_roles)
        self.assertIn('hybrid_pair_worker',hybrid_roles)
        self.assertTrue(full_roles < hybrid_roles)
        self.assertFalse(full['ready_for_board'])

    @unittest.skipUnless(shutil.which('bash'),'Native shell builds require Bash')
    def test_compiler_and_sdk_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)
            compiler=work/'compiler with spaces'
            log=work/'commands.jsonl'
            compiler.write_text('#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["COMPILER_LOG"],"a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n')
            compiler.chmod(0o755)
            script=work/'compile.sh';shutil.copy2(ROOT/'src/native/worker/compile_native.sh',script)
            prefix=work/'sdk with spaces'
            environment={**os.environ,'CC':str(compiler),'CXX':str(compiler),'VART_PREFIX':str(prefix),
                         'ARCH_FLAGS':'','COMPILER_LOG':str(log)}
            result=subprocess.run(['bash',str(script)],env=environment,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            calls=[json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual(len(calls),2)
            self.assertIn('-I'+str(prefix/'include'),calls[1])
            self.assertIn('-L'+str(prefix/'lib'),calls[1])
            self.assertFalse(any(x.startswith('-mcpu=') for row in calls for x in row))
            self.assertTrue(all('-fno-fast-math' in row for row in calls))

if __name__=='__main__':unittest.main()
