import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('board_control',ROOT/'board/control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)

class BoardControlTests(unittest.TestCase):
    def test_dry_run_overrides_config_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'board.json'
            config.write_text(json.dumps({'host':'configured-host','root':'~/configured','front':'/dev/video4'}))
            output=io.StringIO()
            arguments=['control.py','start','--config',str(config),'--host','cli-host',
                       '--root','~/runtime with spaces','--dry-run']
            with mock.patch.object(sys,'argv',arguments),mock.patch.object(control.subprocess,'run') as run,contextlib.redirect_stdout(output):
                self.assertEqual(control.main(),0)
            run.assert_not_called()
            plan=json.loads(output.getvalue())
            self.assertEqual(plan['target'],'ubuntu@cli-host')
            self.assertEqual(plan['settings']['root'],'~/runtime with spaces')
            self.assertEqual(plan['settings']['front'],'/dev/video4')

    def test_remote_root_with_spaces_is_not_shell_interpreted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'runtime with spaces;echo unwanted'
            root.mkdir()
            (root/'control.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            cfg={'root':str(root),'action':'status','mode':'full33'}
            result=subprocess.run([sys.executable,'-c',control.REMOTE],input=json.dumps(cfg),
                                  text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),['status'])

if __name__=='__main__':unittest.main()
