# Usage from any project folder:
#   & "D:\_DEV\Projects\CrewAI\dev_assistant\dev.ps1" "Add /api/health endpoint"
#   & "D:\_DEV\Projects\CrewAI\dev_assistant\dev.ps1" "Add login" "D:\_DEV\Projects\OtherProject"
param(
    [Parameter(Mandatory=$true)][string]$Request,
    [string]$ProjectPath = (Get-Location).Path
)
Push-Location $PSScriptRoot
uv run run_crew $Request $ProjectPath
Pop-Location
