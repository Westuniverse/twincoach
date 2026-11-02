param(
  [Parameter(Mandatory=$true)][string]$BoardIP,
  [Parameter(Mandatory=$true)][string]$BoardRoot,
  [Parameter(Mandatory=$true)][string]$Front,
  [Parameter(Mandatory=$true)][string]$Side,
  [Parameter(Mandatory=$true)][string]$XAuthority,
  [string]$UserName='ubuntu',
  [string]$ConfigOverrides,
  [string]$PreviousSessionFile,
  [switch]$SwapScorerViews,
  [switch]$RecoveryArms,
  [switch]$Yaw60,
  [switch]$DryRun
)
function Quote-Sh([string]$Value) { return "'" + $Value.Replace("'", "'\''") + "'" }
$mapping = if ($SwapScorerViews) { '{"front":"side","side":"front"}' } else { '{"front":"front","side":"side"}' }
$parts=@('python3',(Quote-Sh "$BoardRoot/hybrid_control.py"),'start','--front',(Quote-Sh $Front),
  '--side',(Quote-Sh $Side),'--xauthority',(Quote-Sh $XAuthority),'--scorer-mapping',(Quote-Sh $mapping))
if ($RecoveryArms) { $parts += '--arm-recovery' }
if ($Yaw60) { $parts += '--yaw60' }
if ($ConfigOverrides) { $parts += @('--config-overrides',(Quote-Sh $ConfigOverrides)) }
if ($PreviousSessionFile) { $parts += @('--previous-session-file',(Quote-Sh $PreviousSessionFile)) }
if ($DryRun) { $parts += '--dry-run' }
$encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($parts -join ' ')))
& ssh -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 "$UserName@$BoardIP" "echo $encoded | base64 -d | sh"
exit $LASTEXITCODE
