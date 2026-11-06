param(
    [Parameter(Mandatory=$true)][ValidateSet('Start','Stop','Status')][string]$Action,
    [string]$Python = (Join-Path $PSScriptRoot '../.venv/Scripts/python.exe')
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskState = Join-Path $taskRoot '.scratch/portfolio-closeout'
$taskRegistry = Join-Path $taskState 'processes.json'

if ($Action -eq 'Start') {
    if (-not (Test-Path -LiteralPath (Join-Path $taskState 'private-config.json'))) {
        throw 'Run python -m scripts.portfolio_demo init and prepare first.'
    }
    if (Test-Path -LiteralPath $taskRegistry) {
        $taskPrevious = Get-Content -LiteralPath $taskRegistry -Raw | ConvertFrom-Json
        foreach ($taskItem in $taskPrevious) {
            if (Get-Process -Id $taskItem.pid -ErrorAction SilentlyContinue) {
                throw 'Recorded processes are still present. Run Status/Stop before Start.'
            }
        }
    }
    $taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $taskLogs = Join-Path $taskState "logs/$taskStamp"
    New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
    $taskProcesses = @()
    foreach ($taskRole in @('ops','worker','beat','api','web')) {
        $taskArguments = @('-m','scripts.portfolio_demo',$taskRole,'--state-marker',"`"$taskState`"")
        $taskProcess = Start-Process -FilePath $Python -ArgumentList $taskArguments `
            -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $taskLogs "$taskRole.stdout.log") `
            -RedirectStandardError (Join-Path $taskLogs "$taskRole.stderr.log")
        $taskProcesses += @{
            role=$taskRole; pid=$taskProcess.Id
            started_ticks=$taskProcess.StartTime.ToUniversalTime().Ticks
            logs=$taskLogs
        }
        $taskProcesses | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $taskRegistry -Encoding utf8
    }
    Write-Output 'Started isolated portfolio processes. Verify health/ready before seeding.'
    exit 0
}
if (-not (Test-Path -LiteralPath $taskRegistry)) { throw 'No portfolio process registry exists.' }
$taskProcesses = Get-Content -LiteralPath $taskRegistry -Raw | ConvertFrom-Json
foreach ($taskItem in $taskProcesses) {
    $taskProcess = Get-Process -Id $taskItem.pid -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { Write-Output "$($taskItem.role): stopped"; continue }
    if ($Action -eq 'Status') { Write-Output "$($taskItem.role): PID $($taskItem.pid)"; continue }
    if ($taskProcess.StartTime.ToUniversalTime().Ticks -ne $taskItem.started_ticks) {
        throw 'PID was reused; refusing to stop another process.'
    }
    $taskInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskItem.pid)"
    if (-not $taskInfo.CommandLine.Contains($taskState) -or `
        -not $taskInfo.CommandLine.Contains("scripts.portfolio_demo $($taskItem.role)")) {
        throw 'Process command no longer belongs to this isolated checkout.'
    }
    & taskkill.exe /PID $taskItem.pid /T /F
    if ($LASTEXITCODE -ne 0) { throw 'Portfolio stop failed; inspect the retained process registry.' }
}
