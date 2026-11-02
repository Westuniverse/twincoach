param([Parameter(Mandatory=$true)][string]$BoardIP,
  [Parameter(Mandatory=$true)][string]$BoardRoot,[string]$UserName='ubuntu')
function Quote-Sh([string]$Value) { return "'" + $Value.Replace("'", "'\''") + "'" }
& ssh "$UserName@$BoardIP" ("python3 " + (Quote-Sh "$BoardRoot/control.py") + " stop")
exit $LASTEXITCODE
