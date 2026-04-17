Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Detect OS and architecture
$OS = (Get-CimInstance Win32_OperatingSystem).Caption
$ARCH = (Get-CimInstance Win32_Processor).AddressWidth

Write-Host "Installing Bernstein on $OS ($ARCH-bit)..."

# Select Python launcher (prefer py -3 on Windows)
$pythonMode = ""
$pythonExe = ""

if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonMode = "py"
} else {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if ($pythonCmd) {
        $pythonMode = "exe"
        $pythonExe = $pythonCmd.Source
    }
}

if (-not $pythonMode) {
    Write-Host "Error: Python 3.12+ required. Install from https://www.python.org/"
    exit 1
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$Args)
    if ($pythonMode -eq "py") {
        & py -3 @Args
    } else {
        & $pythonExe @Args
    }
}

try {
    $pythonVersion = Invoke-Python -c "import sys; print('.'.join(str(x) for x in sys.version_info[:3]))"
} catch {
    Write-Host "Error: Python 3.12+ required. Could not run Python."
    exit 1
}

if ([version]$pythonVersion -lt [version]"3.12.0") {
    Write-Host "Error: Python 3.12+ required. Current version: $pythonVersion"
    exit 1
}

try {
    Invoke-Python -m pip --version | Out-Null
} catch {
    Invoke-Python -m ensurepip --upgrade | Out-Null
}

# Get user scripts directory dynamically
$USER_SCRIPTS = Invoke-Python -c "import os, site; print(os.path.join(site.USER_BASE, 'Scripts'))"

function Invoke-Pipx {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$Args)
    $pipxCmd = Get-Command pipx -ErrorAction SilentlyContinue
    if ($pipxCmd) {
        & $pipxCmd.Source @Args
    } else {
        Invoke-Python -m pipx @Args
    }
}

# Install pipx if not present
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    try {
        Invoke-Python -m pipx --version | Out-Null
    } catch {
        Write-Host "pipx not found. Installing..."
        Invoke-Python -m pip install --user --upgrade pipx

        # Ensure pipx paths for future sessions
        Invoke-Python -m pipx ensurepath | Out-Null
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
    if ($pythonMode -eq "py") {
        Write-Host "  py -3 -m pipx ensurepath"
    } else {
        Write-Host "  python -m pipx ensurepath"
    }
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
