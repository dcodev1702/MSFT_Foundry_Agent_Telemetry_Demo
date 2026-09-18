# Author: dcodev1702 (with GitHub Copilot assistance)
# Updated: 2026-09-18
# Purpose: Build the pinned official release wheels when the package mirror lags.
# Usage: Run after notebook environment bootstrap; then rerun package installation.
# Safety: Verify immutable commits, never change upstream source, never delete a
#         checkout, and never install into the repository's root environment.
[CmdletBinding()]
param(
    [string]$SourceRoot = (Join-Path $PSScriptRoot '.source-builds')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Run the notebook virtual-environment bootstrap first.'
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required to retrieve the official tagged release sources.'
}
$wheelhouse = Join-Path $PSScriptRoot '.wheels'
New-Item -ItemType Directory -Path $SourceRoot -Force | Out-Null
New-Item -ItemType Directory -Path $wheelhouse -Force | Out-Null
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path

$releases = @(
    @{
        Name = 'maf-python-1.19.0-source'
        Repository = 'https://github.com/microsoft/agent-framework.git'
        Tag = 'python-1.19.0'
        Commit = '703fbce285ee0f026e5effcadfb9e65aab7f5d84'
        Projects = @('python\packages\core', 'python\packages\openai', 'python\packages\orchestrations', 'python\packages\a2a')
    },
    @{
        Name = 'a2a-sdk-1.1.4-source'
        Repository = 'https://github.com/a2aproject/a2a-python.git'
        Tag = 'v1.1.4'
        Commit = '2d4d3048b245d2af854bad804f0e722ea9febc08'
        Projects = @('.')
    },
    @{
        Name = 'uvicorn-0.53.0-source'
        Repository = 'https://github.com/Kludex/uvicorn.git'
        Tag = '0.53.0'
        Commit = '421708fbc1a704dac8bd4053a7c4c0bf4d3704ee'
        Projects = @('.')
    },
    @{
        Name = 'openai-3.16.0-source'
        Repository = 'https://github.com/openai/openai-python.git'
        Tag = 'v3.16.0'
        Commit = 'dcbd6b8f5c26bc09899a584158729b5b67ba7dc6'
        Projects = @('.')
    },
    @{
        Name = 'httpx2-2.13.0-source'
        Repository = 'https://github.com/pydantic/httpx2.git'
        Tag = 'v2.13.0'
        Commit = 'f2951854442e78cb8f6d2256b7c1d83bd2b77d0b'
        Projects = @('src\httpx2', 'src\httpcore2')
    }
)

foreach ($release in $releases) {
    $checkout = Join-Path $SourceRoot $release.Name
    if (-not (Test-Path -LiteralPath $checkout)) {
        & git clone --depth 1 --branch $release.Tag -- $release.Repository $checkout
        if ($LASTEXITCODE -ne 0) { throw "Git checkout failed: $($release.Name)" }
    }
    $head = & git -C $checkout rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or "$head".Trim() -ne $release.Commit) {
        throw "Release commit mismatch: $checkout. Existing files were not changed."
    }
    $remote = & git -C $checkout remote get-url origin
    if ($LASTEXITCODE -ne 0 -or "$remote".Trim() -ne $release.Repository) {
        throw "Source repository mismatch: $checkout"
    }
    $changes = & git -C $checkout status --porcelain --untracked-files=normal
    if ($LASTEXITCODE -ne 0 -or $changes) {
        throw "Upstream source has local changes: $checkout"
    }
    foreach ($project in $release.Projects) {
        $projectPath = Join-Path $checkout $project
        & $python -m pip wheel --disable-pip-version-check --no-deps --wheel-dir $wheelhouse $projectPath
        if ($LASTEXITCODE -ne 0) { throw "Wheel build failed: $projectPath" }
    }
}

$expected = @(
    'agent_framework_core-1.19.0-py3-none-any.whl',
    'agent_framework_openai-1.14.4-py3-none-any.whl',
    'agent_framework_orchestrations-1.2.0-py3-none-any.whl',
    'agent_framework_a2a-1.0.0b260918-py3-none-any.whl',
    'a2a_sdk-1.1.4-py3-none-any.whl',
    'uvicorn-0.53.0-py3-none-any.whl',
    'openai-3.16.0-py3-none-any.whl',
    'httpx2-2.13.0-py3-none-any.whl',
    'httpcore2-2.13.0-py3-none-any.whl'
)
$wheels = foreach ($name in $expected) {
    $path = Join-Path $wheelhouse $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "The tagged source did not produce the expected wheel: $name"
    }
    @{ File = $name; SHA256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash }
}
@{
    BuiltAt = (Get-Date).ToUniversalTime().ToString('o')
    Releases = $releases
    Wheels = @($wheels)
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $wheelhouse 'source-provenance.json') -Encoding utf8

Write-Output "Verified release wheels are ready in $wheelhouse"
Write-Output 'Rerun the notebook package-installation cell, then restart the kernel.'
