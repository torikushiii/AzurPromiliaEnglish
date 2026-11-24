$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$patcher = Join-Path $root 'patcher'
$dataDir = Join-Path $patcher 'data'
$data = Join-Path $dataDir 'patch-data.bin'
$png = Join-Path $dataDir 'icon.png'
$ico = Join-Path $dataDir 'icon.ico'
if (-not (Test-Path -LiteralPath $data)) { throw "Missing $data. Run export_patch_data.py first." }
if (-not (Test-Path -LiteralPath $png) -and -not (Test-Path -LiteralPath $ico)) { throw "Missing $png or $ico." }

$venv = Join-Path ([IO.Path]::GetTempPath()) 'promilia-patcher-build'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { uv venv $venv --python 3.12 }
& $py -c 'import PyInstaller, PIL' 2>$null
if ($LASTEXITCODE -ne 0) { uv pip install --python $py 'pyinstaller==6.16.0' 'pillow==11.3.0' }

if (Test-Path -LiteralPath $png) {
    & $py -c @"
import sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
im = Image.open(src).convert('RGBA')
side = max(im.size)
canvas = Image.new('RGBA', (side, side), (0, 0, 0, 0))
canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
canvas.save(dst, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
"@ $png $ico
    if ($LASTEXITCODE -ne 0) { throw 'Icon conversion failed.' }
}

$work = Join-Path $venv 'work'
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'AP-English-Patcher' `
    --icon $ico `
    --paths $patcher `
    --add-data "$data;data" `
    --add-data "$ico;data" `
    --distpath (Join-Path $root 'dist') --workpath $work --specpath $work `
    (Join-Path $patcher 'app.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
$exe = Join-Path $root 'dist\AP-English-Patcher.exe'
Write-Host "Built $exe"
Write-Host ("SHA-256 " + (Get-FileHash -LiteralPath $exe).Hash.ToLower())
