# Author: dcodev1702 (with GitHub Copilot assistance)
# Updated: 2026-09-18
# Purpose: Build the Windows Foundry notebook's recent releases from official
#          GitHub source when the approved Python package feed has not admitted them.
# Usage: .\build_notebook_wheels.ps1, then run the notebook package-installation cell.
# Scope: Uses the root .venv for isolated wheel builds, never installs packages,
#        and does not use or modify agent-framework-demo\.venv.
[CmdletBinding()]
param(
    [string]$SourceRoot = (Join-Path $PSScriptRoot '.source-builds'),
    [string]$PythonPath = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

& (Join-Path $PSScriptRoot 'agent-framework-demo\build_source_wheels.ps1') `
    -SourceRoot $SourceRoot `
    -PythonPath $PythonPath `
    -Wheelhouse (Join-Path $PSScriptRoot '.wheels') `
    -ReleaseManifest (Join-Path $PSScriptRoot 'notebook-source-releases.json')

Write-Output 'Runtime: use --find-links .wheels -r requirements-notebook.txt'
Write-Output 'Optional MAF: use --find-links .wheels -r requirements-notebook-shared.txt'
Write-Output 'Validation: use --find-links .wheels -r requirements-notebook-validation.txt'
