param([string]$BoardIP='192.168.137.115',[string]$UserName='ubuntu',
  [string]$BoardRoot='/home/ubuntu/posepriv-demo-20261031',[switch]$LocalOnly)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$failures=0
foreach($name in @('ssh','scp','tar','python')) {
  $command=Get-Command $name -ErrorAction SilentlyContinue
  if (!$command -or $command.Source -like '*sbx-denybin*') {
    Write-Host "[FAIL] $name unavailable in this session"; $failures++
  } else { Write-Host "[OK] $name : $($command.Source)" }
}
foreach($file in @('Runtime/PosePrivFull33Runtime/control.py','Runtime/PosePrivFull33Runtime/runtime_config.template.json')) {
  if (Test-Path -LiteralPath (Join-Path $root $file)) { Write-Host "[OK] $file" }
  else { Write-Host "[FAIL] $file"; $failures++ }
}
if ($LocalOnly) { exit $failures }
if ($BoardRoot -notmatch '^/[a-zA-Z0-9_./-]+$' -or $BoardRoot.Contains('..')) { throw 'Invalid BoardRoot' }
$probe=@'
import glob, importlib.util, json, os, pathlib, platform, subprocess, sys
root=pathlib.Path(sys.argv[1]); checks=[]
def check(name,ok,detail=''):
    checks.append(bool(ok)); print(('[OK] ' if ok else '[FAIL] ')+name+': '+str(detail))
check('architecture',platform.machine()=='aarch64',platform.machine())
check('Python',sys.version_info >= (3,10),platform.python_version())
for name in ('numpy','cv2','PIL'):
    try:
        module=__import__(name); check(name,True,getattr(module,'__version__','installed'))
    except Exception as exc: check(name,False,str(exc))
for item in ('control.py','runtime_config.template.json','candidate/tail4_public_profile_20261031/entry/run_preview.py'):
    check(item,(root/item).is_file())
cameras=glob.glob('/dev/v4l/by-path/*video-index0')
check('two cameras',len(cameras)>=2,cameras)
for item in ('/dev/dri/renderD128','/dev/posepriv_shared_pose_dma','/dev/posepriv_tail4'):
    check(item,pathlib.Path(item).exists())
environment=dict(os.environ,LD_LIBRARY_PATH='/opt/vitis-ai-compat/lib:/lib/aarch64-linux-gnu:/lib')
libraries=subprocess.run(['ldd',str(root/'runtime/tail4_shm_worker')],env=environment,text=True,capture_output=True)
check('native VART/XIR libraries',libraries.returncode==0 and 'not found' not in libraries.stdout and 'libvart-runner' in libraries.stdout and 'libxir' in libraries.stdout,libraries.stdout.strip())
statuses={p:str(pathlib.Path(p).read_text().strip()) for p in glob.glob('/sys/class/drm/*/status')}
check('display connected','connected' in statuses.values(),statuses)
check('CJK font',any(pathlib.Path(p).is_file() for p in ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc','/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf')))
print('[INFO] memory: '+subprocess.check_output(['free','-h'],text=True).strip())
print('[INFO] session: '+str(subprocess.run(['python3',str(root/'control.py'),'status'],text=True,capture_output=True).stdout.strip()))
sys.exit(0 if all(checks) else 1)
'@
$probe | & ssh -o ConnectTimeout=8 "$UserName@$BoardIP" "cd '$BoardRoot' && PYTHONPATH='$BoardRoot/python:$BoardRoot/src' python3 - '$BoardRoot'"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
exit $failures
