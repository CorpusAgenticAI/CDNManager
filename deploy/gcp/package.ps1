# Bundle the files Cloud Shell needs into dist\cdnmanager-gcp.tar.gz.
# tar.exe ships with Windows 10 and later; Compress-Archive on PowerShell 5.1 writes
# backslash paths that unpack as flat file names on Linux.
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")
New-Item -ItemType Directory -Force dist | Out-Null
$archive = "dist/cdnmanager-gcp.tar.gz"
tar.exe -czf $archive --exclude=__pycache__ --exclude=*.pyc `
  pyproject.toml alembic.ini Dockerfile .dockerignore .gcloudignore cloudbuild.yaml app alembic deploy
if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
Write-Host "Created $archive ($([math]::Round((Get-Item $archive).Length / 1KB)) KB)"
