<#
.SYNOPSIS
    Intelligraph setup - installs agent guide, local MCP server, and syncs graph data.
.DESCRIPTION
    This script:
    1. Downloads the agent guide (intelligraph-agent.md)
    2. Updates the opencode plugin (no-op — grep/glob are allowed; guidance is in agent.md)
    3. Downloads the local MCP server script
    4. Syncs graph data from the pod (graph.db + graph.json)
    5. Configures opencode.json or Claude Code MCP settings
    
    After setup, the MCP server runs locally - no HTTP requests to the pod during normal operation.
    Chat still goes through the pod's online pipeline.
    
    Token savings: each search returns 2-3 targeted results (~300 tokens) instead of 16 broad results (~2000 tokens).
    A typical task that used 34k tokens in search alone now uses ~5-6k.
.PARAMETER PodUrl
    Intelligraph pod URL (e.g. http://intelligraph.corp:5050)
.PARAMETER ProjectId
    Project ID in the Intelligraph pod
.PARAMETER RepoDir
    Local repository directory (your project root). Defaults to current directory.
.PARAMETER McpToken
    MCP API token from the Intelligraph UI (Guide tab)
.PARAMETER Harness
    Which harness to configure: "opencode" or "claude". Auto-detected if not specified.
.EXAMPLE
    .\intelligraph-setup.ps1 -PodUrl http://intelligraph.corp:5050 -ProjectId 1 -McpToken abc123
.EXAMPLE
    .\intelligraph-setup.ps1 -PodUrl http://localhost:5050 -ProjectId 1 -McpToken abc123 -Harness opencode -RepoDir .
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$PodUrl,
    [Parameter(Mandatory=$true)]
    [int]$ProjectId,
    [Parameter(Mandatory=$true)]
    [string]$McpToken,
    [string]$RepoDir = ".",
    [ValidateSet("opencode", "claude", "auto")]
    [string]$Harness = "auto",
    [switch]$SslVerify
)

$ErrorActionPreference = "Stop"
$RepoDir = (Resolve-Path $RepoDir).Path
$IntelligraphDir = Join-Path $env:USERPROFILE ".intelligraph"
$CacheDir = Join-Path (Join-Path $IntelligraphDir "cache") "$ProjectId"
$PluginsDir = Join-Path (Join-Path $RepoDir ".opencode") "plugins"

# curl.exe -k flag (skip SSL verification unless -SslVerify was passed)
$curlKFlag = "-k"
if ($SslVerify) { $curlKFlag = "" }

# -- Helpers --
function Write-Step($msg) { Write-Host "`n[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    !   $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "    X   $msg" -ForegroundColor Red }

# -- Detect harness --
if ($Harness -eq "auto") {
    if (Test-Path (Join-Path $RepoDir "opencode.json")) {
        $Harness = "opencode"
    } elseif (Test-Path (Join-Path $RepoDir ".claude")) {
        $Harness = "claude"
    } else {
        Write-Host "`nCould not auto-detect harness. Found neither opencode.json nor .claude/ in $RepoDir" -ForegroundColor Yellow
        $Harness = Read-Host "Enter harness (opencode or claude)"
    }
}
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Intelligraph Setup ($Harness)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Pod:       $PodUrl"
Write-Host "  Project:   $ProjectId"
Write-Host "  Repo:      $RepoDir"
Write-Host "  Token:     $($McpToken.Substring(0, [Math]::Min(8, $McpToken.Length)))..."
Write-Host "  Harness:   $Harness"
Write-Host "========================================" -ForegroundColor Cyan

# -- Create directories --
Write-Step "Creating directories..."
New-Item -ItemType Directory -Force -Path $IntelligraphDir | Out-Null
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
if ($Harness -eq "opencode") {
    New-Item -ItemType Directory -Force -Path $PluginsDir | Out-Null
}
Write-Ok "Directories ready"

# -- Download agent guide --
Write-Step "Downloading agent guide..."
$agentUrl = "$PodUrl/download/agent"
$agentDest = Join-Path $RepoDir "intelligraph-agent.md"
try {
    if ($SslVerify) { curl.exe -s -o "$agentDest" "$agentUrl" } else { curl.exe -k -s -o "$agentDest" "$agentUrl" }
    if (Test-Path $agentDest) {
        Write-Ok "intelligraph-agent.md saved to project root"
    } else {
        Write-Err "Failed to download agent"
    }
} catch {
    Write-Err "Failed to download agent: $_"
}

# -- Download scout subagent (opencode only) --
if ($Harness -eq "opencode") {
    Write-Step "Downloading scout subagent..."
    $scoutUrl = "$PodUrl/download/scout-agent"
    $agentsDir = Join-Path (Join-Path $RepoDir ".opencode") "agents"
    New-Item -ItemType Directory -Force -Path $agentsDir | Out-Null
    $scoutPath = Join-Path $agentsDir "intelligraph-scout.md"
    try {
        if ($SslVerify) { curl.exe -s -o "$scoutPath" "$scoutUrl" } else { curl.exe -k -s -o "$scoutPath" "$scoutUrl" }
        Write-Ok "intelligraph-scout.md saved to .opencode/agents/"
    } catch {
        Write-Warn "Failed to download scout subagent: $_"
    }
}

# -- Download opencode plugin (no-op: grep/glob allowed) --
Write-Step "Updating opencode plugin..."
if ($Harness -eq "opencode") {
    $enforceUrl = "$PodUrl/download/enforce-plugin"
    $enforcePath = Join-Path $PluginsDir "intelligraph-enforce.js"
    try {
        if ($SslVerify) { curl.exe -s -o "$enforcePath" "$enforceUrl" } else { curl.exe -k -s -o "$enforcePath" "$enforceUrl" }
        Write-Ok "intelligraph-enforce.js saved (no-op: grep/glob allowed, guidance in agent.md)"
    } catch {
        Write-Warn "Failed to update plugin: $_"
    }
} else {
    $hooksUrl = "$PodUrl/download/claude-hooks"
    $hooksDir = Join-Path $RepoDir ".claude"
    New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
    $hooksDest = Join-Path $hooksDir "settings.json"
    $hooksContent = $null
    try {
        if ($SslVerify) { curl.exe -s -o "$env:TEMP\claude-hooks.json" "$hooksUrl" } else { curl.exe -k -s -o "$env:TEMP\claude-hooks.json" "$hooksUrl" }
        $hooksContent = Get-Content "$env:TEMP\claude-hooks.json" -Raw | ConvertFrom-Json
        Remove-Item "$env:TEMP\claude-hooks.json" -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Warn "Failed to download hooks: $_"
    }
    if ($hooksContent) {
        if (Test-Path $hooksDest) {
            $existing = Get-Content $hooksDest -Raw | ConvertFrom-Json
            if (-not $existing.PSObject.Properties["hooks"]) {
                $existing | Add-Member -MemberType NoteProperty -Name "hooks" -Value $hooksContent.hooks
            } else {
                $existing.hooks = $hooksContent.hooks
            }
            $existing | ConvertTo-Json -Depth 10 | Set-Content $hooksDest -Encoding UTF8
        } else {
            $hooksContent | ConvertTo-Json -Depth 10 | Set-Content $hooksDest -Encoding UTF8
        }
        Write-Ok "Claude Code hooks updated (no-op: grep/glob allowed)"
    }
}

# -- Download intelligraph MCP server (zip with all deps) --
Write-Step "Downloading intelligraph MCP server..."
$localMcpUrl = "$PodUrl/download/intelligraph-mcp"
$localMcpZip = Join-Path $IntelligraphDir "intelligraph_mcp.zip"
try {
    if ($SslVerify) { curl.exe -s -o "$localMcpZip" "$localMcpUrl" } else { curl.exe -k -s -o "$localMcpZip" "$localMcpUrl" }
    if (Test-Path $localMcpZip) {
        Expand-Archive -Path $localMcpZip -DestinationPath $IntelligraphDir -Force
        Remove-Item $localMcpZip -Force
        $localMcpPath = Join-Path $IntelligraphDir "intelligraph_mcp.py"
        if (Test-Path $localMcpPath) {
            Write-Ok "intelligraph_mcp.py + crg_intelligence.py + model saved to $IntelligraphDir"
        } else {
            Write-Err "intelligraph_mcp.py not found after extraction"
            exit 1
        }
    } else {
        Write-Err "Failed to download intelligraph MCP"
        exit 1
    }
} catch {
    Write-Err "Failed to download intelligraph MCP: $_"
    exit 1
}

# -- Sync graph data from pod --
Write-Step "Syncing graph data from pod (graph.db + graph.json)..."
$syncUrl = "$PodUrl/projects/$ProjectId/sync"
$zipPath = Join-Path $CacheDir "sync.zip"
try {
    if ($SslVerify) {
        curl.exe -s -o "$zipPath" -H "X-MCP-Token: $McpToken" "$syncUrl"
    } else {
        curl.exe -k -s -o "$zipPath" -H "X-MCP-Token: $McpToken" "$syncUrl"
    }
    if (-not (Test-Path $zipPath)) {
        Write-Err "Failed to download sync zip"
        Write-Host "    Check that your MCP token is valid and the project is built." -ForegroundColor Yellow
        exit 1
    }
    # Extract zip (Expand-Archive is native PS 5.0+, -Force overwrites existing files)
    if (Test-Path (Join-Path $CacheDir "graph.db")) { Remove-Item (Join-Path $CacheDir "graph.db") -Force }
    if (Test-Path (Join-Path $CacheDir "graph.json")) { Remove-Item (Join-Path $CacheDir "graph.json") -Force }
    if (Test-Path (Join-Path $CacheDir "metadata.json")) { Remove-Item (Join-Path $CacheDir "metadata.json") -Force }
    Expand-Archive -Path $zipPath -DestinationPath $CacheDir -Force
    Remove-Item $zipPath -Force
    $dbSize = [math]::Round((Get-Item (Join-Path $CacheDir "graph.db")).Length / 1MB, 1)
    Write-Ok "Graph synced (${dbSize}MB) to $CacheDir"
} catch {
    Write-Err "Failed to sync graph data: $_"
    Write-Host "    Check that your MCP token is valid and the project is built." -ForegroundColor Yellow
    exit 1
}

# -- Configure harness --
Write-Step "Configuring $Harness..."

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    $pythonExe = (Get-Command python3 -ErrorAction SilentlyContinue).Source
}
if (-not $pythonExe) {
    Write-Err "Python not found. Install Python 3.10+ and add to PATH."
    exit 1
}

if ($Harness -eq "opencode") {
    $opencodePath = Join-Path $RepoDir "opencode.json"
    # PS 5.1: ConvertFrom-Json returns PSCustomObject, not hashtable
    $config = $null
    if (Test-Path $opencodePath) {
        $config = Get-Content $opencodePath -Raw | ConvertFrom-Json
    }
    if (-not $config) { $config = [PSCustomObject]@{} }
    
    # Build MCP config — command is an ARRAY (executable + all args), no separate args field
    $cmdArgs = @(
        $pythonExe,
        $localMcpPath,
        "--pod-url", $PodUrl,
        "--project-id", "$ProjectId",
        "--repo-dir", $RepoDir,
        "--mcp-token", $McpToken
    )
    $mcpEntry = [PSCustomObject]@{
        type = "local"
        command = $cmdArgs
        enabled = $true
        timeout = 120000
    }
    
    # Add or update mcp.intelligraph
    if (-not $config.PSObject.Properties["mcp"]) {
        $config | Add-Member -MemberType NoteProperty -Name "mcp" -Value ([PSCustomObject]@{})
    }
    if ($config.mcp.PSObject.Properties["intelligraph"]) {
        $config.mcp.intelligraph = $mcpEntry
    } else {
        $config.mcp | Add-Member -MemberType NoteProperty -Name "intelligraph" -Value $mcpEntry
    }
    
    # NOTE: The plugin at .opencode/plugins/intelligraph-enforce.js
    # is AUTO-LOADED by opencode at startup. It is a NO-OP — grep/glob
    # are allowed. Agent guidance is in intelligraph-agent.md.
    # Do NOT add it to the "plugin" key — that key is for npm package names
    # only. See https://opencode.ai/docs/plugins
    
    $config | ConvertTo-Json -Depth 10 | Set-Content $opencodePath -Encoding UTF8
    Write-Ok "opencode.json configured with intelligraph MCP"
} else {
    # Claude Code — uses .mcp.json at project root
    $mcpConfigPath = Join-Path $RepoDir ".mcp.json"
    $mcpConfig = @{
        mcpServers = @{
            intelligraph = @{
                command = $pythonExe
                args = @(
                    $localMcpPath,
                    "--pod-url", $PodUrl,
                    "--project-id", "$ProjectId",
                    "--repo-dir", $RepoDir,
                    "--mcp-token", $McpToken
                )
            }
        }
    }
    $mcpConfig | ConvertTo-Json -Depth 10 | Set-Content $mcpConfigPath -Encoding UTF8
    Write-Ok ".mcp.json saved to project root (Claude Code)"
}

# -- Create mcp-update command --
Write-Step "Creating mcp-update command..."
$updateScript = @"
# Intelligraph MCP update - re-syncs graph data from pod (atomic)
`$PodUrl = "$PodUrl"
`$ProjectId = $ProjectId
`$McpToken = "$McpToken"
`$CacheDir = Join-Path (Join-Path (Join-Path `$env:USERPROFILE ".intelligraph") "cache") "`$ProjectId"

Write-Host "[*] Re-syncing graph data from `$PodUrl..." -ForegroundColor Cyan
`$zipPath = Join-Path `$CacheDir "sync.zip"
`$tmpDir = Join-Path `$CacheDir "_tmp_update"
try {
    curl.exe -k -s -o `$zipPath -H "X-MCP-Token: `$McpToken" "`$PodUrl/projects/`$ProjectId/sync"
    if (-not (Test-Path `$zipPath)) {
        Write-Host "[X] Update failed: download error" -ForegroundColor Red
        exit 1
    }
    # Extract to temp dir first (atomic - don't touch live files yet)
    if (Test-Path `$tmpDir) { Remove-Item `$tmpDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path `$tmpDir | Out-Null
    Expand-Archive -Path `$zipPath -DestinationPath `$tmpDir -Force
    # Move files from temp to cache (atomic per-file)
    Move-Item -Path (Join-Path `$tmpDir "graph.db") -Destination (Join-Path `$CacheDir "graph.db") -Force
    Move-Item -Path (Join-Path `$tmpDir "graph.json") -Destination (Join-Path `$CacheDir "graph.json") -Force
    Move-Item -Path (Join-Path `$tmpDir "metadata.json") -Destination (Join-Path `$CacheDir "metadata.json") -Force -ErrorAction SilentlyContinue
    # Touch: update mtime so the running MCP detects the change
    (Get-ChildItem (Join-Path `$CacheDir "graph.db")).LastWriteTime = Get-Date
    (Get-ChildItem (Join-Path `$CacheDir "graph.json")).LastWriteTime = Get-Date
    # Cleanup
    Remove-Item `$zipPath -Force
    Remove-Item `$tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "[OK] Graph data updated. The running MCP will pick up changes on next search." -ForegroundColor Green
} catch {
    Write-Host "[X] Update failed: `$_" -ForegroundColor Red
    if (Test-Path `$tmpDir) { Remove-Item `$tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
}
"@
$updatePath = Join-Path $IntelligraphDir "mcp-update.ps1"
$updateScript | Set-Content $updatePath -Encoding UTF8

# Add to PowerShell profile if not already there
$profilePath = $PROFILE.CurrentUserAllHosts
$profileDir = Split-Path $profilePath -Parent
if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Force -Path $profileDir | Out-Null }
if (Test-Path $profilePath) {
    $profileContent = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue
} else {
    $profileContent = ""
}
if ($profileContent -notmatch "mcp-update") {
    $aliasLine = "function mcp-update { & '$updatePath' }"
    Add-Content $profilePath $aliasLine
    Write-Ok "mcp-update command available (restart shell or run: . `$PROFILE)"
} else {
    Write-Ok "mcp-update already in profile"
}

# -- Create /mcp-update slash command (opencode or Claude Code) --
Write-Step "Creating /mcp-update slash command..."
$commandContent = @"
---
description: Re-sync graph data from Intelligraph pod
---
Re-syncing graph data from the pod...

!``powershell -ExecutionPolicy Bypass -File "$updatePath"``

The graph data has been updated. The MCP server will pick up changes automatically on the next search.
"@
if ($Harness -eq "opencode") {
    $commandsDir = Join-Path (Join-Path $RepoDir ".opencode") "commands"
    if (-not (Test-Path $commandsDir)) { New-Item -ItemType Directory -Force -Path $commandsDir | Out-Null }
    $commandPath = Join-Path $commandsDir "mcp-update.md"
    $commandContent | Set-Content $commandPath -Encoding UTF8
    Write-Ok "/mcp-update slash command saved to .opencode/commands/"
} elseif ($Harness -eq "claude") {
    $commandsDir = Join-Path (Join-Path $RepoDir ".claude") "commands"
    if (-not (Test-Path $commandsDir)) { New-Item -ItemType Directory -Force -Path $commandsDir | Out-Null }
    $commandPath = Join-Path $commandsDir "mcp-update.md"
    $claudeContent = @"
---
description: Re-sync graph data from Intelligraph pod
shell: powershell
---
Re-syncing graph data from the pod...

!``powershell -ExecutionPolicy Bypass -File "$updatePath"``

The graph data has been updated. The MCP server will pick up changes automatically on the next search.
"@
    $claudeContent | Set-Content $commandPath -Encoding UTF8
    Write-Ok "/mcp-update slash command saved to .claude/commands/"
}

# -- Summary --
Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Agent guide:     $RepoDir\intelligraph-agent.md"
Write-Host "  Scout subagent:  $(if ($Harness -eq 'opencode') { $RepoDir + '\.opencode\agents\intelligraph-scout.md' } else { 'N/A (opencode only)' })"
Write-Host "  Plugin:          $(if ($Harness -eq 'opencode') { $PluginsDir + '\intelligraph-enforce.js (no-op)' } else { $RepoDir + '\.claude\settings.json (no-op)' })"
Write-Host "  Intelligraph MCP: $localMcpPath"
Write-Host "  Graph cache:     $CacheDir"
Write-Host "  MCP update:      mcp-update (re-syncs graph from pod)"
Write-Host ""
Write-Host "  The MCP server runs locally. Chat still uses the pod's online pipeline."
Write-Host "  Run 'mcp-update' after rebuilding a project to re-sync graph data."
Write-Host "========================================" -ForegroundColor Green
