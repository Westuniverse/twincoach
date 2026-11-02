param(
  [string]$BoardIP='', [string]$UserName='', [string]$BoardRoot='',
  [string]$Front='', [string]$Side='', [string]$XAuthority='', [string]$Display='',
  [string]$Config='', [string]$Profile='', [string]$CommandFile='',
  [ValidateSet('Full33','Hybrid')][string]$Mode='Full33',
  [int]$Port=0, [switch]$SwapViews, [switch]$DryRun, [switch]$LocalOnly
)
$cliArgs=@("$PSScriptRoot/control.py",'start')
$fields=@{BoardIP='host';UserName='user';BoardRoot='root';Front='front';Side='side';
 XAuthority='xauthority';Display='display';Config='config';Profile='profile';CommandFile='command-file'}
foreach($name in $fields.Keys) {
  $value=Get-Variable -Name $name -ValueOnly
  if ($value) { $cliArgs+=@("--$($fields[$name])",$value) }
}
if ($Port) { $cliArgs+=@('--port',"$Port") }
if ($PSBoundParameters.ContainsKey('Mode')) { $cliArgs+=@('--mode',$Mode.ToLower()) }
if ($SwapViews) { $cliArgs+='--swap-views' }
if ($DryRun) { $cliArgs+='--dry-run' }
if ($LocalOnly) { $cliArgs+='--local' }
& python @cliArgs
exit $LASTEXITCODE
