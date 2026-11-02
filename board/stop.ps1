param(
  [string]$BoardIP='192.168.137.115',
  [string]$BoardRoot='/home/ubuntu/posepriv-demo-20261031',
  [string]$UserName='ubuntu'
)
& "$PSScriptRoot/stop-run.ps1" -BoardIP $BoardIP -BoardRoot $BoardRoot -UserName $UserName
exit $LASTEXITCODE
