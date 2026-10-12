# Build the standalone Windows version of bigprint.
#
#   .\packaging\build_windows.ps1
#
# Produces:
#   dist\bigprint\                         ready-to-run folder (bigprint.exe, bigprint-cli.exe)
#   dist\bigprint-<version>-windows-x64.zip  the same folder, zipped for sharing
#
# Needs the dev virtual environment with PyInstaller:
#   .venv\Scripts\python -m pip install -e . pyinstaller

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

$version = & $py -c "import bigprint; print(bigprint.__version__)"
Write-Host "Building bigprint $version..."

& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\bigprint.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$app = Join-Path $root "dist\bigprint"

# Smoke test: the packaged program must read every supported input format.
# (Some readers rely on lazily imported libraries that a build can miss.)
Write-Host "Smoke-testing every input format with the packaged bigprint-cli.exe..."
$smoke = Join-Path $root "build\smoke"
if (Test-Path $smoke) { Remove-Item $smoke -Recurse -Force }
$samples = & $py packaging\smoke_samples.py $smoke
foreach ($sample in $samples) {
    $out = "$sample-out"
    & (Join-Path $app "bigprint-cli.exe") $sample --height 300 --printer bambu-x1 --dry-run --no-preview -o $out *> "$sample.log"
    if ($LASTEXITCODE -ne 0) {
        Get-Content "$sample.log" -Tail 5
        throw "Packaged build failed to process $(Split-Path $sample -Leaf)"
    }
    Write-Host "  ok: $(Split-Path $sample -Leaf)"
}

& $py packaging\collect_licenses.py $app | Out-Null
Copy-Item LICENSE (Join-Path $app "licenses\bigprint-LICENSE.txt")
Copy-Item packaging\README-windows.txt (Join-Path $app "README.txt")

$zip = Join-Path $root "dist\bigprint-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "Done: $app"
Write-Host "      $zip ($size MB)"
