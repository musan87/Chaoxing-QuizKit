$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (Test-Path 'build') { Remove-Item -Recurse -Force 'build' }
if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }

pyinstaller --clean --noconfirm Chaoxing-QuizKit.spec

$exePath = Join-Path $PSScriptRoot 'dist\Chaoxing-QuizKit.exe'
if (Test-Path $exePath) {
    $sizeMB = [math]::Round((Get-Item $exePath).Length / 1MB, 2)
    Write-Host "Build succeeded: $exePath ($sizeMB MB)" -ForegroundColor Green
} else {
    throw "Build failed: $exePath not found"
}
