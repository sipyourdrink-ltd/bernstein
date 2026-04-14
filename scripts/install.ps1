# Detect OS and architecture
$OS = (Get-CimInstance -ClassName Win32_OperatingSystem).Caption
$ARCH = (Get-CimInstance -ClassName Win32_Processor).AddressWidth

Write-Host "Installing Bernstein on $OS ($ARCH-bit)..."

# Check for Python 3.12+
if (-not (Get-Command python3 -ErrorAction SilentlyContinue)) {
    Write-Host "Error: Python 3.12+ required. Install from python.org"
    exit 1
}

$PYTHON_VERSION = (python3 --version).Split(" ")[1]
if ([version]$PYTHON_VERSION -lt [version]"3.12") {
    Write-Host "Error: Python 3.12+ required. Current version: $PYTHON_VERSION"
    exit 1
}

# Install pipx if not present
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Host "Installing pipx..."
    python3 -m pip install --user pipx
    python3 -m pipx ensurepath
    $env:Path += ";$HOME\.local\bin"
}

# Install Bernstein
pipx install bernstein

Write-Host ""
Write-Host "Bernstein installed! Run: bernstein --version"
Write-Host "Get started: bernstein -g 'your goal here'"