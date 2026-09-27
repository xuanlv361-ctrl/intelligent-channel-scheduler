param([switch]$OpenBrowser)
# Backward-compatible wrapper. The supported entry point uses a hyphen.
& (Join-Path $PSScriptRoot "start-local.ps1") -OpenBrowser:$OpenBrowser
exit $LASTEXITCODE
