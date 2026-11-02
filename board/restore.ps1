param(
  [string]$BoardIP='192.168.137.115',
  [string]$BoardRoot='/home/ubuntu/posepriv-demo-20261031',
  [string]$UserName='ubuntu',
  [string]$P4CommandFile='/home/ubuntu/posepriv-di46-ddr200-20261031/p4-matched/p4-command.matched.private.json'
)
& "$PSScriptRoot/restore-p4.ps1" -BoardIP $BoardIP -BoardRoot $BoardRoot -UserName $UserName -P4CommandFile $P4CommandFile
exit $LASTEXITCODE
