[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ManifestExternalId = 'ed77d99f-074b-4ef6-9fbc-55bfeb7b5aef',
    [switch]$KeepCatalogEntry,
    [switch]$KeepUserInstall
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'teams-chat.ps1')

if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Authentication)) {
    Write-Host 'Installing Microsoft.Graph.Authentication module...'
    Install-Module Microsoft.Graph.Authentication -Scope CurrentUser -Force
}

Import-Module Microsoft.Graph.Authentication

$requiredGraphScopes = @(
    'User.Read'
)

if (-not $KeepUserInstall) {
    $requiredGraphScopes += 'TeamsAppInstallation.ReadWriteSelfForUser'
}

if (-not $KeepCatalogEntry) {
    $requiredGraphScopes += 'AppCatalog.ReadWrite.All'
}

$ctx = Connect-FoundryGraphIfNeeded -Scopes $requiredGraphScopes

if ($ctx.Account -notmatch '@dibsecurity\.onmicrosoft\.com$') {
    throw 'Teams app cleanup requires a Microsoft Graph connection in the dibsecurity.onmicrosoft.com tenant.'
}

function Invoke-FoundryGraphJsonGet {
    param(
        [Parameter(Mandatory)]
        [string]$Uri
    )

    $response = Invoke-MgGraphRequest -Method GET -Uri $Uri

    if ($null -eq $response) {
        return $null
    }

    if ($response -is [string]) {
        $trimmedResponse = $response.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmedResponse)) {
            return $null
        }

        if ($trimmedResponse.StartsWith('{') -or $trimmedResponse.StartsWith('[')) {
            return $trimmedResponse | ConvertFrom-Json -Depth 20
        }

        throw "Unexpected Microsoft Graph response payload: $trimmedResponse"
    }

    if ($response -is [System.Collections.IDictionary]) {
        return [pscustomobject]$response
    }

    $response
}

$encodedFilter = [System.Uri]::EscapeDataString("externalId eq '$ManifestExternalId'")
$catalogResponse = Invoke-FoundryGraphJsonGet -Uri "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps?`$filter=$encodedFilter"
$catalogEntries = @($catalogResponse.value)

if ($catalogEntries.Count -eq 0) {
    Write-Host "No Teams app catalog entry found for external ID $ManifestExternalId."
    return
}

$catalogEntry = $catalogEntries[0]
$catalogAppId = $catalogEntry.id

Write-Host "Found Teams catalog entry: $catalogAppId"
if ($catalogEntry.displayName) {
    Write-Host "Catalog display name: $($catalogEntry.displayName)"
}

if (-not $KeepUserInstall) {
    $installedAppsResponse = Invoke-FoundryGraphJsonGet -Uri 'https://graph.microsoft.com/v1.0/me/teamwork/installedApps?$expand=teamsAppDefinition'
    $installedApps = @($installedAppsResponse.value) | Where-Object {
        $_.teamsAppDefinition.teamsAppId -eq $catalogAppId
    }

    if ($installedApps.Count -eq 0) {
        Write-Host "No current-user Teams installation found for catalog app $catalogAppId."
    } else {
        foreach ($installedApp in $installedApps) {
            $installId = $installedApp.id
            if ($PSCmdlet.ShouldProcess("me/teamwork/installedApps/$installId", 'Delete Teams user app installation')) {
                Invoke-MgGraphRequest -Method DELETE -Uri "https://graph.microsoft.com/v1.0/me/teamwork/installedApps/$installId" | Out-Null
                Write-Host "Removed current-user Teams app installation: $installId"
            }
        }
    }
}

if (-not $KeepCatalogEntry) {
    if ($PSCmdlet.ShouldProcess("appCatalogs/teamsApps/$catalogAppId", 'Delete Teams app catalog entry')) {
        Invoke-MgGraphRequest -Method DELETE -Uri "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/$catalogAppId" | Out-Null
        Write-Host "Removed Teams app catalog entry: $catalogAppId"
    }
}

Write-Host 'Teams app cleanup completed.'