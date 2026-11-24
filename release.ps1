$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
& (Join-Path $root 'build_exe.ps1')

$py = Join-Path ([IO.Path]::GetTempPath()) 'promilia-patcher-build\Scripts\python.exe'
$info = & $py -c "import sys; sys.path.insert(0, sys.argv[1]); import engine; p = engine.load_patch_data(); print(p.version); print(p.game_label)" (Join-Path $root 'patcher')
if ($LASTEXITCODE -ne 0) { throw 'Could not read the patch version.' }
$version, $gameLabel = $info
$tag = "v$version"

$exe = Join-Path $root 'dist\AP-English-Patcher.exe'
$outDir = Join-Path $root 'release'
$zip = Join-Path $outDir "AP-English-Patcher-$tag.zip"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
Compress-Archive -LiteralPath $exe -DestinationPath $zip -CompressionLevel Optimal
Remove-Item -Recurse -Force (Join-Path $root 'patcher\__pycache__') -ErrorAction SilentlyContinue

$exeHash = (Get-FileHash -LiteralPath $exe).Hash.ToLower()
$zipHash = (Get-FileHash -LiteralPath $zip).Hash.ToLower()
$notes = Join-Path $outDir "notes-$tag.md"
@"
English patch $version for Azur Promilia JP (PC), $gameLabel.

Extract the zip, close the game, run ``AP-English-Patcher.exe`` and click **Install English Patch**.

> [!WARNING]
> Use at your own risk. We are not responsible for any action taken against your account as a result of using this translation tool.

| File | SHA-256 |
| --- | --- |
| AP-English-Patcher.exe | ``$exeHash`` |
| AP-English-Patcher-$tag.zip | ``$zipHash`` |
"@ | Set-Content -LiteralPath $notes -Encoding utf8

Write-Host ""
Write-Host "Tag:   $tag, for $gameLabel"
Write-Host "Zip:   $zip"
Write-Host "Notes: $notes"
Write-Host "exe SHA-256 $exeHash"
Write-Host "zip SHA-256 $zipHash"
Write-Host ""
Write-Host "Publish with:"
Write-Host "  gh release create $tag `"$zip`" --title `"English Patch $tag`" --notes-file `"$notes`""
