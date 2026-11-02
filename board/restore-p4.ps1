param([Parameter(Mandatory=$true)][string]$BoardIP,
  [Parameter(Mandatory=$true)][string]$BoardRoot,
  [Parameter(Mandatory=$true)][string]$P4CommandFile,[string]$UserName='ubuntu')
function Quote-Sh([string]$Value) { return "'" + $Value.Replace("'", "'\''") + "'" }
& ssh "$UserName@$BoardIP" ("python3 " + (Quote-Sh "$BoardRoot/control.py") +
  " rollback --p4-command-file " + (Quote-Sh $P4CommandFile))
exit $LASTEXITCODE
