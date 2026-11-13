param(
    [Parameter(Mandatory=$true)][ValidateSet('Start','Stop','Status')][string]$Action,
    [string]$Python = (Join-Path $PSScriptRoot '../.venv/Scripts/python.exe'),
    [ValidateRange(5,300)][int]$ReadyTimeoutSeconds = 90
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskState = Join-Path $taskRoot '.scratch/portfolio-closeout'
$taskRegistry = Join-Path $taskState 'processes.json'

function Stop-PortfolioProcess($taskItem) {
    $taskProcess = Get-Process -Id $taskItem.pid -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { return }
    if ($taskProcess.StartTime.ToUniversalTime().Ticks -ne $taskItem.started_ticks) {
        throw 'PID was reused; refusing to stop another process.'
    }
    $taskInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskItem.pid)"
    if (-not $taskInfo.CommandLine.Contains($taskState) -or `
        -not $taskInfo.CommandLine.Contains("scripts.portfolio_demo $($taskItem.role)")) {
        throw 'Process command no longer belongs to this isolated checkout.'
    }
    & taskkill.exe /PID $taskItem.pid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Portfolio stop failed; inspect the retained process registry.' }
}

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
    # Bind and release before spawning anything; an unrelated listener must
    # never satisfy our readiness checks or be stopped by failure cleanup.
    foreach ($taskPort in @(8040,3040,8946)) {
        $taskListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$taskPort)
        try { $taskListener.Start() }
        catch { throw "Portfolio port $taskPort is occupied; no processes started. Existing role logs: $taskState/logs" }
        finally { $taskListener.Stop() }
    }
    & $Python -m scripts.portfolio_demo secure-state
    if ($LASTEXITCODE -ne 0) { throw 'Portfolio permission hardening failed; no processes started.' }
    $taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $taskLogs = Join-Path $taskState "logs/$taskStamp"
    New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
    $taskProcesses = @()
    $taskProbe = $null
    try {
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
      $taskProbe = Start-Process -FilePath $Python `
          -ArgumentList @('-m','scripts.portfolio_readiness','--timeout-seconds',"$ReadyTimeoutSeconds") `
          -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru `
          -RedirectStandardOutput (Join-Path $taskLogs 'readiness.stdout.log') `
          -RedirectStandardError (Join-Path $taskLogs 'readiness.stderr.log')
      $taskDeadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
      while ($true) {
          foreach ($taskItem in $taskProcesses) {
              $taskAlive = Get-Process -Id $taskItem.pid -ErrorAction SilentlyContinue
              if ($null -eq $taskAlive) { throw "Portfolio role $($taskItem.role) exited before readiness." }
              if ($taskAlive.StartTime.ToUniversalTime().Ticks -ne $taskItem.started_ticks) {
                  throw "Portfolio role $($taskItem.role) PID was replaced before readiness."
              }
          }
          $taskProbe.Refresh()
          if ($taskProbe.HasExited) {
              $taskProbe.WaitForExit()
              if ($taskProbe.ExitCode -ne 0) {
                  $taskFailure = Get-Content -LiteralPath (Join-Path $taskLogs 'readiness.stderr.log') -Tail 1
                  throw "Portfolio readiness failed: $taskFailure"
              }
              break
          }
          if ([DateTime]::UtcNow -ge $taskDeadline) { throw 'Portfolio readiness deadline exceeded.' }
          Start-Sleep -Milliseconds 200
      }
      Write-Output "Portfolio ready: API, Web proxy/page, Ops MCP handshake/tools, dedicated worker pong; all five processes alive. Logs: $taskLogs"
      exit 0
    } catch {
      $taskFailure = $_.Exception.Message
      if ($null -ne $taskProbe -and -not $taskProbe.HasExited) {
          & taskkill.exe /PID $taskProbe.Id /T /F | Out-Null
      }
      foreach ($taskItem in $taskProcesses) {
          try { Stop-PortfolioProcess $taskItem }
          catch { Write-Warning "Cleanup retained $($taskItem.role): $($_.Exception.Message)" }
      }
      throw "$taskFailure Role/probe logs: $taskLogs"
    }
}
if (-not (Test-Path -LiteralPath $taskRegistry)) { throw 'No portfolio process registry exists.' }
$taskProcesses = Get-Content -LiteralPath $taskRegistry -Raw | ConvertFrom-Json
foreach ($taskItem in $taskProcesses) {
    $taskProcess = Get-Process -Id $taskItem.pid -ErrorAction SilentlyContinue
    if ($null -eq $taskProcess) { Write-Output "$($taskItem.role): stopped"; continue }
    if ($Action -eq 'Status') { Write-Output "$($taskItem.role): PID $($taskItem.pid)"; continue }
    Stop-PortfolioProcess $taskItem
}
