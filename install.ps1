<#
Bootstrap installer for patent-checker (Windows PowerShell 5.1 and PowerShell 7).

Usage:
    powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.ps1 | iex"

This script only installs uv (if missing) and the patent-checker CLI via
"uv tool install". It does NOT show the consent notice, place the Skill or
register the MCP server -- that all happens interactively when you run
"patent-checker install" (this script runs it for you automatically when
stdin is not redirected; otherwise it just prints the command to run next).

Environment variables:
    PATENT_CHECKER_SPEC   Spec passed to "uv tool install" (default:
                          patent-checker). Examples:
                            git+https://github.com/xhighhongo41/patent-checker@v0.5.0
                            .
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Usage {
    @'
Usage: install.ps1 [-DryRun] [-Help]

  -DryRun  Show what would be done without installing anything.
  -Help    Show this help message and exit.

Environment variables:
  PATENT_CHECKER_SPEC  Spec passed to "uv tool install" (default: patent-checker).
'@
}

if ($Help) {
    Show-Usage
    exit 0
}

$spec = $env:PATENT_CHECKER_SPEC
if ([string]::IsNullOrEmpty($spec)) {
    $spec = 'patent-checker'
}

function Invoke-OrShow {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArgs
    )
    if ($DryRun) {
        Write-Host "would run: $($CommandArgs -join ' ')"
        return
    }
    $exe = $CommandArgs[0]
    $rest = $CommandArgs[1..($CommandArgs.Length - 1)]
    & $exe @rest
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

# Step 1: make sure uv is available.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    Write-Host "uv found: $($uv.Source)"
} else {
    Write-Host 'uv not found'
    Write-Host 'Installing uv (https://astral.sh/uv)'
    if ($DryRun) {
        Write-Host 'would run: irm https://astral.sh/uv/install.ps1 | iex'
    } else {
        irm https://astral.sh/uv/install.ps1 | iex
    }
    $localBin = Join-Path $env:USERPROFILE '.local\bin'
    if ($DryRun) {
        Write-Host "would add: $localBin to PATH"
    } else {
        $env:Path = "$localBin;$env:Path"
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) {
            Write-Host 'uv still not found; open a new shell and run this script again.'
            exit 1
        }
    }
}

# Step 2: install (or upgrade) the CLI.
Invoke-OrShow -CommandArgs @('uv', 'tool', 'install', '--upgrade', $spec)

# Step 3: confirm the CLI is on PATH and report its version.
Invoke-OrShow -CommandArgs @('patent-checker', '--version')

# Step 4: hand off to the interactive installer, when possible.
if ($DryRun) {
    Write-Host 'would run: patent-checker install (if stdin is not redirected)'
    Write-Host "would print: Next: run 'patent-checker install' in your project directory (it shows the notice, installs the Skill and registers the MCP server) (if stdin is redirected)"
    exit 0
}

if (-not [Console]::IsInputRedirected) {
    & patent-checker install
    exit $LASTEXITCODE
} else {
    Write-Host "Next: run 'patent-checker install' in your project directory (it shows the notice, installs the Skill and registers the MCP server)"
    exit 0
}
