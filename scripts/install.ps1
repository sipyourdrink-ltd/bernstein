Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Detect OS and architecture
$OS = (Get-CimInstance Win32_OperatingSystem).Caption
$ARCH = (Get-CimInstance Win32_Processor).AddressWidth

Write-Host "Installing Bernstein on $OS ($ARCH-bit)..."

# Detect Python (python or python3)
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
}

if (-not $pythonCmd) {
    Write-Host "Error: Python 3.12+ required. Install from https://www.python.org/"
    exit 1
}

$pythonExe = $pythonCmd.Source
$pythonVersion = & $pythonExe -c "import sys; print('.'.join(str(x) for x in sys.version_info[:3]))"

if ([version]$pythonVersion -lt [version]"3.12.0") {
    Write-Host "Error: Python 3.12+ required. Current version: $pythonVersion"
    exit 1
}

try {
    & $pythonExe -m pip --version | Out-Null
} catch {
    & $pythonExe -m ensurepip --upgrade | Out-Null
}

# Get user scripts directory dynamically
$USER_SCRIPTS = & $pythonExe -c "import os, site; print(os.path.join(site.USER_BASE, 'Scripts'))"

function Invoke-Pipx {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$Args)
    $pipxCmd = Get-Command pipx -ErrorAction SilentlyContinue
    if ($pipxCmd) {
        & $pipxCmd.Source @Args
    } else {
        & $pythonExe -m pipx @Args
    }
}

# Install pipx if not present
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    try {
        & $pythonExe -m pipx --version | Out-Null
    } catch {
    Write-Host "pipx not found. Installing..."
        & $pythonExe -m pip install --user --upgrade pipx

        # Ensure pipx paths for future sessions
        & $pythonExe -m pipx ensurepath | Out-Null
    }
}

# Add scripts directory to CURRENT session
if (-not ($env:Path -split ";" | Where-Object { $_ -eq $USER_SCRIPTS })) {
    $env:Path += ";$USER_SCRIPTS"
}

# Verify pipx works
try {
    Invoke-Pipx --version | Out-Null
} catch {
    Write-Host "Error: pipx installed but not found in PATH."
    Write-Host "Try restarting your terminal or running:"
    Write-Host "  python -m pipx ensurepath"
    exit 1
}

# Install or upgrade Bernstein
Write-Host "Installing Bernstein..."
try {
    Invoke-Pipx install bernstein
} catch {
    Invoke-Pipx upgrade bernstein
}

Write-Host ""
Write-Host "Bernstein installed successfully!"
Write-Host ""
Write-Host "Try:"
Write-Host "  bernstein --version"
Write-Host "  bernstein -g 'your goal here'"
