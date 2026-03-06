# Deploy (or clean up) Azure AI Foundry Environment
# Pre-req: Az PowerShell, Microsoft.Graph PowerShell, Azure CLI with Bicep
# Usage:
#   .\deploy-foundry-env.ps1              # deploy all resources + RBAC
#   .\deploy-foundry-env.ps1 -Cleanup     # tear down resources + RBAC (keeps Entra group)
param(
    [switch]$Cleanup
)

$ErrorActionPreference = "Stop"

# ── Configuration ──
$subscriptionId         = (Get-AzSubscription -SubscriptionName "zolab").Id
$securitySubscriptionId = (Get-AzSubscription -SubscriptionName "Security").Id
$location               = "eastus2"
$groupDisplayName       = "zolab-ai-dev"
$defaultModelCapacity   = 250

function Write-BuildStatus {
    param(
        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$StorageAccountName,

        [Parameter(Mandatory)]
        [string]$KeyVaultName,

        [Parameter(Mandatory)]
        [string]$AppInsightsName,

        [Parameter(Mandatory)]
        [string]$AiFoundryName,

        [Parameter(Mandatory)]
        [string]$AiProjectName,

        [Parameter(Mandatory)]
        [string]$GenAiModelDisplay,

        [Parameter(Mandatory)]
        [string]$FoundryProjectEndpoint,

        [Parameter(Mandatory)]
        [string]$AzureOpenAIEndpoint,

        [Parameter(Mandatory)]
        [string]$AppInsightsConnectionStatus,

        [Parameter(Mandatory)]
        [string]$LawRbacStatus,

        [Parameter(Mandatory)]
        [string]$UserStatus
    )

    $rows = @(
        @{ Item = '☁️ Resource Group'; Status = "✅ $ResourceGroupName" },
        @{ Item = '🗄️ Storage'; Status = $StorageAccountName },
        @{ Item = '🔐 Key Vault'; Status = $KeyVaultName },
        @{ Item = '📊 App Insights'; Status = $AppInsightsName },
        @{ Item = '🤖 AI Foundry'; Status = $AiFoundryName },
        @{ Item = '🏢 AI Project'; Status = $AiProjectName },
        @{ Item = '🧠 Model'; Status = $GenAiModelDisplay },
        @{ Item = '🔗 App Insights Connection'; Status = $AppInsightsConnectionStatus },
        @{ Item = '📡 LAW RBAC'; Status = $LawRbacStatus },
        @{ Item = '👤 User'; Status = $UserStatus },
        @{ Item = '🔌 Foundry Project Endpoint'; Status = $FoundryProjectEndpoint },
        @{ Item = '🤖 Azure OpenAI Endpoint'; Status = $AzureOpenAIEndpoint }
    )

    $itemWidth = [Math]::Max(29, (($rows | ForEach-Object { $_.Item.Length } | Measure-Object -Maximum).Maximum + 2))
    $statusWidth = [Math]::Max(78, (($rows | ForEach-Object { $_.Status.Length } | Measure-Object -Maximum).Maximum + 2))

    $lines = @(
        ''
        '● ☁️🎉🚀 Fresh build — all green!'
        ''
        ("┌" + ("─" * $itemWidth) + "┬" + ("─" * $statusWidth) + "┐")
        ("│" + " Item".PadRight($itemWidth) + "│" + " Status".PadRight($statusWidth) + "│")
        ("├" + ("─" * $itemWidth) + "┼" + ("─" * $statusWidth) + "┤")
    )

    foreach ($row in $rows) {
        $lines += ("│" + (" " + $row.Item).PadRight($itemWidth) + "│" + (" " + $row.Status).PadRight($statusWidth) + "│")
    }

    $lines += ("└" + ("─" * $itemWidth) + "┴" + ("─" * $statusWidth) + "┘")
    $lines += ''
    $lines += 'Ready for the notebook! 🎯'

    $lines
}

function Write-BuildInfoJson {
    param(
        [Parameter(Mandatory)]
        [string]$OutputPath,

        [Parameter(Mandatory)]
        [string]$ResourceGroupName,

        [Parameter(Mandatory)]
        [string]$AppInsightsName,

        [Parameter(Mandatory)]
        [string]$FoundryProjectEndpoint,

        [Parameter(Mandatory)]
        [string]$AzureOpenAIEndpoint,

        [Parameter(Mandatory)]
        [string]$StorageAccountName,

        [Parameter(Mandatory)]
        [string]$KeyVaultName,

        [Parameter(Mandatory)]
        [string]$GenAiModel,

        [Parameter(Mandatory)]
        [string]$AiFoundryName,

        [Parameter(Mandatory)]
        [string]$AiProjectName
    )

    $buildInfo = [ordered]@{
        rg                      = $ResourceGroupName
        appinsights             = $AppInsightsName
        foundry_project_endpoint = $FoundryProjectEndpoint
        azure_openai_endpoint   = $AzureOpenAIEndpoint
        storage_account         = $StorageAccountName
        key_vault               = $KeyVaultName
        genai_model             = $GenAiModel
        foundry_name            = $AiFoundryName
        foundry_project_name    = $AiProjectName
    }

    $buildInfo | ConvertTo-Json | Set-Content -Path $OutputPath -Encoding utf8
}

function Get-AllowedAiModelChoices {
    @(
        "gpt-4.1-mini"
        "gpt-5.3"
        "gpt-5.4"
        "grok-4-1-fast-reasoning"
    )
}

function Read-AiModelSelection {
    param(
        [Parameter(Mandatory)]
        [string[]]$Choices
    )

    if (-not $Choices) {
        throw "No AI model choices are available."
    }

    $options = for ($i = 0; $i -lt $Choices.Count; $i++) {
        [System.Management.Automation.Host.ChoiceDescription]::new(
            "&$($i + 1) $($Choices[$i])",
            "Deploy $($Choices[$i])"
        )
    }

    $selectionIndex = $Host.UI.PromptForChoice(
        "Select AI model",
        "Choose one of the allowed AI models for deployment.",
        $options,
        0
    )

    $Choices[$selectionIndex]
}

function Get-LocationModelCatalog {
    param(
        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    if (
        $script:locationModelCatalog -and
        $script:locationModelCatalogLocation -eq $Location -and
        $script:locationModelCatalogSubscriptionId -eq $SubscriptionId
    ) {
        return $script:locationModelCatalog
    }

    $catalogJson = az cognitiveservices model list `
        --location $Location `
        --subscription $SubscriptionId `
        --only-show-errors `
        --output json 2>&1

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to list available AI models in $($Location):`n$($catalogJson -join "`n")"
    }

    $script:locationModelCatalog = $catalogJson | ConvertFrom-Json
    $script:locationModelCatalogLocation = $Location
    $script:locationModelCatalogSubscriptionId = $SubscriptionId

    $script:locationModelCatalog
}

function Get-AiModelMatchPattern {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice
    )

    switch ($ModelChoice) {
        "gpt-4.1-mini" { '^gpt-4\.1-mini$' }
        "gpt-5.3" { '^gpt-5\.3($|-)' }
        "gpt-5.4" { '^gpt-5\.4($|-)' }
        "grok-4-1-fast-reasoning" { '^grok-4-1-fast-reasoning$' }
        default { throw "Unsupported AI model choice '$ModelChoice'." }
    }
}

function Get-AiModelFormat {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice
    )

    switch ($ModelChoice) {
        "grok-4-1-fast-reasoning" { 'xAI' }
        default { 'OpenAI' }
    }
}

function Get-AiModelCandidate {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId
    )

    $matchPattern = Get-AiModelMatchPattern -ModelChoice $ModelChoice
    $modelFormat = Get-AiModelFormat -ModelChoice $ModelChoice
    $catalog = Get-LocationModelCatalog -Location $Location -SubscriptionId $SubscriptionId

    $catalog |
        Where-Object {
            $_.kind -eq 'AIServices' -and
            $_.model.format -eq $modelFormat -and
            $_.lifecycleStatus -ne 'Deprecated' -and
            $_.model.name -match $matchPattern
        } |
        Sort-Object `
            @{ Expression = { if ($_.model.name -eq $ModelChoice) { 0 } else { 1 } } }, `
            @{ Expression = { $_.model.version }; Descending = $true } |
        Select-Object -First 1
}

function Get-AiModelCapacityOptions {
    param(
        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [string]$ModelFormat,

        [Parameter(Mandatory)]
        [string]$ModelName,

        [Parameter(Mandatory)]
        [string]$ModelVersion
    )

    $armToken = az account get-access-token `
        --resource https://management.azure.com `
        --query accessToken `
        --output tsv `
        --only-show-errors 2>&1

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to acquire Azure Resource Manager token:`n$($armToken -join "`n")"
    }

    $headers = @{
        Authorization = "Bearer $($armToken -join '')"
    }

    $url = "https://management.azure.com/subscriptions/$SubscriptionId/providers/Microsoft.CognitiveServices/locations/$Location/modelCapacities?api-version=2024-10-01&modelFormat=$([uri]::EscapeDataString($ModelFormat))&modelName=$([uri]::EscapeDataString($ModelName))&modelVersion=$([uri]::EscapeDataString($ModelVersion))"
    $response = Invoke-RestMethod -Method Get -Uri $url -Headers $headers

    @($response.value)
}

function Select-AiModelSku {
    param(
        [Parameter(Mandatory)]
        [object[]]$CapacityOptions,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity
    )

    $availableOptions = @(
        $CapacityOptions |
            Where-Object { $_.properties.availableCapacity -gt 0 }
    )

    if (-not $availableOptions) {
        return $null
    }

    $preferredSkuNames = @("GlobalStandard", "Standard")
    $chosenOption = $null

    foreach ($preferredSkuName in $preferredSkuNames) {
        $chosenOption = $availableOptions |
            Where-Object { $_.properties.skuName -eq $preferredSkuName } |
            Sort-Object @{ Expression = { $_.properties.availableCapacity }; Descending = $true } |
            Select-Object -First 1

        if ($chosenOption) {
            break
        }
    }

    if (-not $chosenOption) {
        $chosenOption = $availableOptions |
            Sort-Object @{ Expression = { $_.properties.availableCapacity }; Descending = $true } |
            Select-Object -First 1
    }

    $deployCapacity = [Math]::Max(
        1,
        [Math]::Min($DefaultCapacity, [int][Math]::Floor($chosenOption.properties.availableCapacity))
    )

    [pscustomobject]@{
        SkuName   = $chosenOption.properties.skuName
        Capacity  = $deployCapacity
        Available = [int][Math]::Floor($chosenOption.properties.availableCapacity)
    }
}

function Resolve-AiModelSpecification {
    param(
        [Parameter(Mandatory)]
        [string]$ModelChoice,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity
    )

    $candidate = Get-AiModelCandidate `
        -ModelChoice $ModelChoice `
        -Location $Location `
        -SubscriptionId $SubscriptionId

    if (-not $candidate) {
        return $null
    }

    $modelFormat = Get-AiModelFormat -ModelChoice $ModelChoice

    $capacitySelection = Select-AiModelSku `
        -CapacityOptions (Get-AiModelCapacityOptions `
            -Location $Location `
            -SubscriptionId $SubscriptionId `
            -ModelFormat $modelFormat `
            -ModelName $candidate.model.name `
            -ModelVersion $candidate.model.version) `
        -DefaultCapacity $DefaultCapacity

    if (-not $capacitySelection) {
        return $null
    }

    [pscustomobject]@{
        RequestedChoice = $ModelChoice
        DeploymentName  = $ModelChoice
        ModelFormat     = $modelFormat
        ModelName       = $candidate.model.name
        ModelVersion    = $candidate.model.version
        SkuName         = $capacitySelection.SkuName
        SkuCapacity     = $capacitySelection.Capacity
        AvailableCapacity = $capacitySelection.Available
        LifecycleStatus = $candidate.lifecycleStatus
    }
}

function Select-DeployableAiModel {
    param(
        [Parameter(Mandatory)]
        [string[]]$AllowedChoices,

        [Parameter(Mandatory)]
        [string]$Location,

        [Parameter(Mandatory)]
        [string]$SubscriptionId,

        [Parameter(Mandatory)]
        [int]$DefaultCapacity
    )

    $remainingChoices = [System.Collections.Generic.List[string]]::new()
    foreach ($choice in $AllowedChoices) {
        [void]$remainingChoices.Add($choice)
    }

    $currentChoice = $null

    while ($remainingChoices.Count -gt 0) {
        if (-not $currentChoice) {
            $currentChoice = Read-AiModelSelection -Choices $remainingChoices.ToArray()
        }

        Write-Host "Checking AI model availability for '$currentChoice' in $Location..."

        $modelSpec = Resolve-AiModelSpecification `
            -ModelChoice $currentChoice `
            -Location $Location `
            -SubscriptionId $SubscriptionId `
            -DefaultCapacity $DefaultCapacity

        if ($modelSpec) {
            return $modelSpec
        }

        Write-Warning "'$currentChoice' is not currently deployable in $Location. Choose another model."
        [void]$remainingChoices.Remove($currentChoice)
        $currentChoice = $null
    }

    throw "None of the allowed AI models are currently deployable in $Location."
}

Write-Host "Resolved subscriptions:"
Write-Host "  zolab    : $subscriptionId"
Write-Host "  Security : $securitySubscriptionId"

# ── 1. Ensure Microsoft.Graph.Groups module ──
if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Groups)) {
    Write-Host "Installing Microsoft.Graph.Groups module..."
    Install-Module Microsoft.Graph.Groups -Scope CurrentUser -Force
}
Import-Module Microsoft.Graph.Groups

# ── 2. Connect to Microsoft Graph (if needed) ──
$ctx = Get-MgContext
if (-not $ctx) {
    Write-Host "Connecting to Microsoft Graph..."
    Connect-MgGraph -Scopes "Group.ReadWrite.All","GroupMember.ReadWrite.All"
}

# ── 3. Set Azure subscription context (both Az PowerShell and az CLI) ──
Set-AzContext -SubscriptionId $subscriptionId | Out-Null
az account set --subscription $subscriptionId 2>&1 | Out-Null
Write-Host "Subscription set to zolab ($subscriptionId)"

# ════════════════════════════════════════════════════════════════
#  CLEANUP MODE
# ════════════════════════════════════════════════════════════════
if ($Cleanup) {
    Write-Host ""
    Write-Host "=== CLEANUP MODE ==="

    # ── Resolve the zolab-ai-dev group ──
    $group = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
    if ($group) {
        $groupObjectId = $group.Id
        Write-Host "Found Entra group '$groupDisplayName' — ObjectId: $groupObjectId"

        # ── Remove all RBAC role assignments for the group across the subscription ──
        Write-Host "Removing RBAC role assignments for '$groupDisplayName'..."
        $assignments = Get-AzRoleAssignment -ObjectId $groupObjectId -ErrorAction SilentlyContinue
        foreach ($a in $assignments) {
            Write-Host "  Removing: $($a.RoleDefinitionName) @ $($a.Scope)"
            Remove-AzRoleAssignment -ObjectId $groupObjectId `
                -RoleDefinitionName $a.RoleDefinitionName `
                -Scope $a.Scope `
                -ErrorAction SilentlyContinue
        }
        if (-not $assignments) { Write-Host "  No role assignments found." }

        # ── Remove the deploying user from the group ──
        $currentUser = Get-MgContext
        $userId = (Get-MgUser -UserId $currentUser.Account).Id
        $isMember = Get-MgGroupMember -GroupId $groupObjectId | Where-Object { $_.Id -eq $userId }
        if ($isMember) {
            Remove-MgGroupMemberByRef -GroupId $groupObjectId -DirectoryObjectId $userId
            Write-Host "Removed current user ($($currentUser.Account)) from '$groupDisplayName'"
        } else {
            Write-Host "Current user ($($currentUser.Account)) is not a member of '$groupDisplayName'"
        }
    } else {
        Write-Host "Entra group '$groupDisplayName' not found — skipping RBAC cleanup."
    }

    # ── Remove resource groups matching zolab-ai-<suffix> (not the original zolab-ai) ──
    $rgList = Get-AzResourceGroup | Where-Object { $_.ResourceGroupName -match '^zolab-ai-.{4,}$' }
    foreach ($rg in $rgList) {
        Write-Host "Deleting resource group '$($rg.ResourceGroupName)'..."
        # Purge Cognitive Services to avoid soft-delete conflicts on redeploy
        $cogAccounts = Get-AzResource -ResourceGroupName $rg.ResourceGroupName `
            -ResourceType "Microsoft.CognitiveServices/accounts" -ErrorAction SilentlyContinue
        foreach ($cog in $cogAccounts) {
            Write-Host "  Will purge Cognitive Services account '$($cog.Name)' after RG deletion."
        }
        Remove-AzResourceGroup -Name $rg.ResourceGroupName -Force
        # Purge soft-deleted Cognitive Services accounts
        foreach ($cog in $cogAccounts) {
            Write-Host "  Purging soft-deleted account '$($cog.Name)'..."
            az cognitiveservices account purge `
                --name $cog.Name `
                --resource-group $rg.ResourceGroupName `
                --location $location `
                --subscription $subscriptionId 2>&1 | Out-Null
        }
    }
    if (-not $rgList) { Write-Host "No zolab-ai-<suffix> resource groups found." }

    # ── Remove subscription-level deployment records (zolab) ──
    $deployments = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'foundry-ai-env*' }
    foreach ($d in $deployments) {
        Write-Host "Removing deployment record '$($d.DeploymentName)'..."
        Remove-AzSubscriptionDeployment -Name $d.DeploymentName -ErrorAction SilentlyContinue
    }

    # ── Remove LAW RBAC role assignment + deployment record from Security subscription ──
    $securitySubId = $securitySubscriptionId
    Write-Host "Cleaning up LAW RBAC in Security subscription..."
    Set-AzContext -SubscriptionId $securitySubId | Out-Null

    # Remove the actual role assignment on DIBSecCom workspace
    if ($group) {
        $lawScope = "/subscriptions/$securitySubId/resourceGroups/Sentinel/providers/Microsoft.OperationalInsights/workspaces/DIBSecCom"
        $lawAssignments = Get-AzRoleAssignment -ObjectId $groupObjectId -Scope $lawScope -ErrorAction SilentlyContinue
        foreach ($a in $lawAssignments) {
            Write-Host "  Removing: $($a.RoleDefinitionName) @ $($a.Scope)"
            Remove-AzRoleAssignment -ObjectId $groupObjectId `
                -RoleDefinitionName $a.RoleDefinitionName `
                -Scope $a.Scope `
                -ErrorAction SilentlyContinue
        }
        if (-not $lawAssignments) { Write-Host "  No LAW role assignments found." }
    }

    # Remove deployment records
    $lawDeploys = Get-AzSubscriptionDeployment | Where-Object { $_.DeploymentName -like 'law-rbac*' }
    foreach ($d in $lawDeploys) {
        Write-Host "Removing deployment record '$($d.DeploymentName)'..."
        Remove-AzSubscriptionDeployment -Name $d.DeploymentName -ErrorAction SilentlyContinue
    }

    # Restore zolab context
    Set-AzContext -SubscriptionId $subscriptionId | Out-Null

    $buildInfoPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'build_info.json'
    if (Test-Path -LiteralPath $buildInfoPath) {
        Remove-Item -LiteralPath $buildInfoPath -Force
        Write-Host "Removed stale build info file '$buildInfoPath'."
    } else {
        Write-Host "No build_info.json file found to remove."
    }

    Write-Host ""
    Write-Host "=== Cleanup complete ==="
    exit 0
}

# ════════════════════════════════════════════════════════════════
#  DEPLOY MODE
# ════════════════════════════════════════════════════════════════

# ── Create or retrieve the zolab-ai-dev Entra security group ──
$existingGroup = Get-MgGroup -Filter "displayName eq '$groupDisplayName'" -ErrorAction SilentlyContinue
if ($existingGroup) {
    Write-Host "Entra group '$groupDisplayName' already exists — ObjectId: $($existingGroup.Id)"
    $groupObjectId = $existingGroup.Id
} else {
    Write-Host "Creating Entra security group '$groupDisplayName'..."
    $newGroup = New-MgGroup `
        -DisplayName $groupDisplayName `
        -MailEnabled:$false `
        -SecurityEnabled:$true `
        -MailNickname "zolab-ai-dev"
    $groupObjectId = $newGroup.Id
    Write-Host "Created group '$groupDisplayName' — ObjectId: $groupObjectId"
}

# ── Add the deploying user to the zolab-ai-dev group ──
$currentUser = Get-MgContext
$userId = (Get-MgUser -UserId $currentUser.Account).Id
$isMember = Get-MgGroupMember -GroupId $groupObjectId | Where-Object { $_.Id -eq $userId }
if ($isMember) {
    Write-Host "Current user ($($currentUser.Account)) is already a member of '$groupDisplayName'"
} else {
    New-MgGroupMember -GroupId $groupObjectId -DirectoryObjectId $userId
    Write-Host "Added current user ($($currentUser.Account)) to '$groupDisplayName'"
}

# ── Select and validate AI model ──
$selectedAiModelSpec = Select-DeployableAiModel `
    -AllowedChoices (Get-AllowedAiModelChoices) `
    -Location $location `
    -SubscriptionId $subscriptionId `
    -DefaultCapacity $defaultModelCapacity

Write-Host "AI model selection:"
Write-Host "  Requested option : $($selectedAiModelSpec.RequestedChoice)"
Write-Host "  Resolved model   : $($selectedAiModelSpec.ModelName) [$($selectedAiModelSpec.ModelFormat)] $($selectedAiModelSpec.ModelVersion)"
Write-Host "  Deployment SKU   : $($selectedAiModelSpec.SkuName) x $($selectedAiModelSpec.SkuCapacity)"

# ── Generate random 6-char alphanumeric suffix ──
$suffix = -join ((48..57) + (97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
Write-Host "Generated suffix: $suffix"
Write-Host "Resource group will be: zolab-ai-$suffix"

# ── Deploy Bicep (subscription-scoped via az cli) ──
Write-Host ""
Write-Host "Deploying AI Foundry environment..."
$deployOutput = az deployment sub create `
    --location $location `
    --template-file "$PSScriptRoot\main.bicep" `
    --name "foundry-ai-env-$suffix" `
    --parameters `
        aiDevGroupObjectId=$groupObjectId `
        securitySubscriptionId=$securitySubscriptionId `
        suffix=$suffix `
        aiModelDeploymentName=$($selectedAiModelSpec.DeploymentName) `
        aiModelName=$($selectedAiModelSpec.ModelName) `
        aiModelFormat=$($selectedAiModelSpec.ModelFormat) `
        aiModelVersion=$($selectedAiModelSpec.ModelVersion) `
        aiModelSkuName=$($selectedAiModelSpec.SkuName) `
        aiModelSkuCapacity=$($selectedAiModelSpec.SkuCapacity) `
    --output json 2>&1

# Separate warnings/stderr from JSON output
$rawOutput = $deployOutput -join "`n"
# Extract the JSON object by finding the first { and last }
$jsonStart = $rawOutput.IndexOf('{')
$jsonEnd = $rawOutput.LastIndexOf('}')
if ($jsonStart -ge 0 -and $jsonEnd -gt $jsonStart) {
    $jsonString = $rawOutput.Substring($jsonStart, $jsonEnd - $jsonStart + 1)
} else {
    Write-Error "No JSON found in deployment output:`n$rawOutput"
    exit 1
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Deployment failed:`n$rawOutput"
    exit 1
}

$result = $jsonString | ConvertFrom-Json

Write-Host ""
Write-Host "=== Deployment Result ==="
Write-Host "ProvisioningState        : $($result.properties.provisioningState)"
Write-Host "Resource Group           : $($result.properties.outputs.resourceGroupName.value)"
Write-Host "Suffix                   : $($result.properties.outputs.suffix.value)"
Write-Host "Storage Account          : $($result.properties.outputs.storageAccountName.value)"
Write-Host "Key Vault                : $($result.properties.outputs.keyVaultName.value)"
Write-Host "App Insights             : $($result.properties.outputs.appInsightsName.value)"
Write-Host "AI Foundry               : $($result.properties.outputs.aiFoundryName.value)"
Write-Host "AI Project               : $($result.properties.outputs.aiProjectName.value)"
Write-Host "AI Model Deployment      : $($result.properties.outputs.aiModelDeploymentName.value)"
Write-Host "Foundry Project Endpoint : $($result.properties.outputs.foundryProjectEndpoint.value)"
Write-Host "Azure OpenAI Endpoint    : $($result.properties.outputs.azureOpenAIEndpoint.value)"
Write-Host ""
Write-Host "Portal link:"
Write-Host "https://portal.azure.com/#@/resource/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)/overview"

# ── Deploy LAW RBAC to Security subscription (Log Analytics Reader on DIBSecCom) ──
$securitySubId = $securitySubscriptionId
Write-Host ""
Write-Host "Deploying Log Analytics Reader RBAC to Security subscription..."
az account set --subscription $securitySubId 2>&1 | Out-Null

$lawOutput = az deployment sub create `
    --location $location `
    --template-file "$PSScriptRoot\law-rbac.bicep" `
    --name "law-rbac-zolab-ai-dev" `
    --parameters aiDevGroupObjectId=$groupObjectId `
    --output json 2>&1

# Capture exit code before running any other commands
$lawExitCode = $LASTEXITCODE

$lawJson = $lawOutput | Where-Object { $_ -notmatch '^WARNING:|^BCP\d|\.bicep\(' }
$lawWarnings = $lawOutput | Where-Object { $_ -match '^WARNING:|^BCP\d|\.bicep\(' }
if ($lawWarnings) { $lawWarnings | ForEach-Object { Write-Host $_ } }

# Restore zolab subscription context
az account set --subscription $subscriptionId 2>&1 | Out-Null

if ($lawExitCode -ne 0) {
    Write-Error "LAW RBAC deployment failed:`n$($lawOutput -join "`n")"
    exit 1
}

Write-Host "  Log Analytics Reader assigned to '$groupDisplayName' on DIBSecCom workspace."

$connectionId = "/subscriptions/$subscriptionId/resourceGroups/$($result.properties.outputs.resourceGroupName.value)/providers/Microsoft.CognitiveServices/accounts/$($result.properties.outputs.aiFoundryName.value)/projects/$($result.properties.outputs.aiProjectName.value)/connections/$($result.properties.outputs.aiFoundryName.value)-appinsights"
$connectionSharedToAll = az resource show `
    --ids $connectionId `
    --api-version 2025-06-01 `
    --query properties.isSharedToAll `
    --output tsv 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to resolve App Insights connection scope:`n$($connectionSharedToAll -join "`n")"
    exit 1
}

$appInsightsConnectionStatus = if (($connectionSharedToAll -join '').Trim().ToLowerInvariant() -eq 'true') {
    'Shared to all projects ✅'
} else {
    'This project only ✅'
}

$buildInfoPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'build_info.json'
Write-BuildInfoJson `
    -OutputPath $buildInfoPath `
    -ResourceGroupName $result.properties.outputs.resourceGroupName.value `
    -AppInsightsName $result.properties.outputs.appInsightsName.value `
    -FoundryProjectEndpoint $result.properties.outputs.foundryProjectEndpoint.value `
    -AzureOpenAIEndpoint $result.properties.outputs.azureOpenAIEndpoint.value `
    -StorageAccountName $result.properties.outputs.storageAccountName.value `
    -KeyVaultName $result.properties.outputs.keyVaultName.value `
    -GenAiModel $selectedAiModelSpec.DeploymentName `
    -AiFoundryName $result.properties.outputs.aiFoundryName.value `
    -AiProjectName $result.properties.outputs.aiProjectName.value
Write-Host "📝 Build info written to $buildInfoPath"

Write-BuildStatus `
    -ResourceGroupName $result.properties.outputs.resourceGroupName.value `
    -StorageAccountName $result.properties.outputs.storageAccountName.value `
    -KeyVaultName $result.properties.outputs.keyVaultName.value `
    -AppInsightsName $result.properties.outputs.appInsightsName.value `
    -AiFoundryName $result.properties.outputs.aiFoundryName.value `
    -AiProjectName $result.properties.outputs.aiProjectName.value `
    -GenAiModelDisplay "$($selectedAiModelSpec.DeploymentName) ($($selectedAiModelSpec.SkuName))" `
    -FoundryProjectEndpoint $result.properties.outputs.foundryProjectEndpoint.value `
    -AzureOpenAIEndpoint $result.properties.outputs.azureOpenAIEndpoint.value `
    -AppInsightsConnectionStatus $appInsightsConnectionStatus `
    -LawRbacStatus 'Log Analytics Reader on DIBSecCom ✅' `
    -UserStatus "$($currentUser.Account) added to $groupDisplayName ✅"
