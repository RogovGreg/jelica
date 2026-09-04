Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$cliPackageDir = Join-Path $repoRoot "apps/cli"

function Get-UvVersionValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VersionOutput
    )

    $tokens = $VersionOutput -split '\s+'
    if ($tokens.Length -ge 2) {
        return $tokens[1]
    }
    return $VersionOutput
}

function Write-UvAvailableMessage {
    $versionOutput = (& uv --version).Trim()
    $version = Get-UvVersionValue -VersionOutput $versionOutput
    Write-Host "uv $version is available."
}

function Add-PathEntryIfMissing {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathEntry
    )

    if ([string]::IsNullOrWhiteSpace($PathEntry)) {
        return
    }

    $pathEntries = $env:PATH -split ';'
    if (-not ($pathEntries -contains $PathEntry)) {
        $env:PATH = "$PathEntry;$env:PATH"
    }
}

function Add-StandardUvDirsToPath {
    $candidateDirs = @()
    if (-not [string]::IsNullOrWhiteSpace($HOME)) {
        $candidateDirs += (Join-Path $HOME ".local\bin")
        $candidateDirs += (Join-Path $HOME ".cargo\bin")
    }
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $candidateDirs += (Join-Path $env:USERPROFILE ".local\bin")
        $candidateDirs += (Join-Path $env:USERPROFILE ".cargo\bin")
    }

    foreach ($candidateDir in ($candidateDirs | Select-Object -Unique)) {
        $uvWithExe = Join-Path $candidateDir "uv.exe"
        $uvWithoutExe = Join-Path $candidateDir "uv"
        if ((Test-Path $uvWithExe) -or (Test-Path $uvWithoutExe)) {
            Add-PathEntryIfMissing -PathEntry $candidateDir
        }
    }
}

function Ensure-UvAvailable {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-UvAvailableMessage
        return
    }

    Write-Host "uv was not found. Installing uv using the official Astral standalone installer..."
    try {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    }
    catch {
        Write-Error "Failed to install uv automatically. Install it manually from https://docs.astral.sh/uv/getting-started/installation/ and rerun install.ps1."
        exit 1
    }

    Add-StandardUvDirsToPath

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "uv was installed but is still not available in PATH. Open a new terminal and rerun install.ps1."
        exit 1
    }

    Write-UvAvailableMessage
}

Ensure-UvAvailable

if (-not (Test-Path (Join-Path $cliPackageDir "pyproject.toml"))) {
    Write-Error "CLI package was not found at '$cliPackageDir'."
    exit 1
}

Write-Host "Installing jelica-cli as a global uv tool..."
uv tool install --directory "$repoRoot" --editable "$cliPackageDir" --force --reinstall

$uvToolBinDir = (uv tool dir --bin).Trim()
$currentPathEntries = $env:PATH -split ';'
if (-not ($currentPathEntries -contains $uvToolBinDir)) {
    $env:PATH = "$uvToolBinDir;$env:PATH"
    Write-Host "Added '$uvToolBinDir' to PATH for this PowerShell session."
    Write-Host "Open a new terminal to persist PATH changes."
}

if (-not (Get-Command jelica -ErrorAction SilentlyContinue)) {
    Write-Error "'jelica' command is still not available after installation."
    exit 1
}

$configPath = (jelica config path).Trim()
Write-Host "System config path: $configPath"

if (-not (Test-Path $configPath)) {
    Write-Host "System config is missing. Initializing with defaults..."
    jelica config init --non-interactive
}
else {
    Write-Host "System config already exists. Skipping initialization."
}

Write-Host "Verifying CLI version..."
jelica --version

Write-Host "Installation completed successfully."
