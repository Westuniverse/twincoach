param(
  [string]$BoardIP='192.168.137.115',
  [string]$BoardRoot='/home/ubuntu/posepriv-demo-20261031',
  [string]$UserName='ubuntu',
  [string]$Front='/dev/v4l/by-path/platform-xhci-hcd.0.auto-usb-0:1.1:1.0-video-index0',
  [string]$Side='/dev/v4l/by-path/platform-xhci-hcd.0.auto-usb-0:1.3:1.0-video-index0',
  [string]$XAuthority='/run/user/1000/gdm/Xauthority',
  [string]$PreviousSessionFile='',
  [ValidateSet('Full33','Hybrid')][string]$Mode='Full33',
  [switch]$Quality,
  [switch]$Fast,
  [switch]$DryRun
)
$profile = if ($Fast) { "$BoardRoot/fast-overrides.json" } elseif ($Quality) { "$BoardRoot/quality-overrides.json" } else { "$BoardRoot/demo-overrides.json" }
$arguments=@{ BoardIP=$BoardIP; BoardRoot=$BoardRoot; UserName=$UserName; Front=$Front; Side=$Side;
 XAuthority=$XAuthority; ConfigOverrides=$profile; SwapScorerViews=$true }
if ($PreviousSessionFile) { $arguments.PreviousSessionFile=$PreviousSessionFile }
if ($DryRun) { $arguments.DryRun=$true }
$launcher = if ($Mode -eq 'Hybrid') { 'start-hybrid.ps1' } else { 'start-full.ps1' }
& "$PSScriptRoot/$launcher" @arguments
exit $LASTEXITCODE
