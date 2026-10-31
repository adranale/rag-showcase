<#
.SYNOPSIS
    Creates the Azure AI Document Intelligence account used for OCR (BILLABLE).

.DESCRIPTION
    Creates `di-rag-<Suffix>` (kind FormRecognizer, SKU S0, pay per analysed page)
    in the existing resource group. The account has no idle cost, but every OCR'd
    page is billed and a quota is NOT a monetary cap: check the Azure pricing page
    and your billing/credits before ingesting large document sets.

    The key is not printed or stored by this script. The ingestion job script
    (deploy/azure-ingest-job.ps1, also run by the "Azure ingestion job" workflow)
    looks the account up by name and passes endpoint and key to the job as a
    Container Apps secret. Run the ingestion job again afterwards.

    Safe to rerun: an existing account is left untouched.

.EXAMPLE
    pwsh ./deploy/azure-ocr.ps1 -SubscriptionId <guid> -Suffix ragdemo01
#>
#Requires -Version 7.0
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')][string]$SubscriptionId,
    [ValidatePattern('^[A-Za-z0-9._()-]{1,90}$')][string]$ResourceGroup = 'rg-rag-showcase',
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{3,10}$')][string]$Suffix,
    [ValidatePattern('^[a-z0-9]{3,30}$')][string]$Location = 'francecentral',
    [switch]$AssumeYes
)

$ErrorActionPreference = 'Stop'
$accountName = "di-rag-$Suffix"

# Runs `az <args>`; $Label is a safe description used in errors (arguments are not printed).
function Invoke-Az {
    $label, $azArgs = $args
    if ($azArgs[0] -notin 'extension', 'version', 'rest') { $azArgs += '--subscription', $SubscriptionId }
    $output = & az @azArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $lines = @($output | ForEach-Object { "$_" } | Where-Object { $_.Trim() })
        $detail = ($lines | Where-Object { $_ -match '^(ERROR|error:)|\(\w+\)' } | Select-Object -First 6) -join "`n"
        throw "Azure CLI step failed: $label (exit code $LASTEXITCODE).`n$detail"
    }
    ($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
        ForEach-Object { "$_" }) -join "`n" | ForEach-Object { $_.Trim() }
}

Write-Host '== Preflight (nothing is changed yet) =='
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) is not installed.' }
try {
    $sub = Invoke-Az 'resolve subscription' account show --query '{name:name,id:id,tenant:tenantId,state:state}' --output json | ConvertFrom-Json
}
catch {
    throw "Subscription $SubscriptionId is not accessible with the current 'az login'."
}
if ($sub.state -ne 'Enabled') { throw "Subscription $SubscriptionId is in state '$($sub.state)', not Enabled." }

$groupExists = Invoke-Az 'check resource group' group exists --name $ResourceGroup
if ($groupExists -ne 'true') { throw "Resource group '$ResourceGroup' does not exist in this subscription." }

$provider = Invoke-Az 'check Microsoft.CognitiveServices provider' provider show `
    --namespace Microsoft.CognitiveServices --query registrationState --output tsv
if ($provider -ne 'Registered') {
    throw "Resource provider Microsoft.CognitiveServices is '$provider'. Register it first: az provider register --namespace Microsoft.CognitiveServices --subscription $SubscriptionId"
}

$existing = Invoke-Az 'check for existing account' cognitiveservices account list `
    --resource-group $ResourceGroup --query "[?name=='$accountName'].name" --output tsv
if ($existing -eq $accountName) {
    Write-Host "Account '$accountName' already exists; nothing to do."
    Write-Host 'Run the Azure ingestion job again so the job picks up the endpoint and key.'
    return
}

Write-Host ''
Write-Host "  Subscription:    $($sub.name)"
Write-Host "  Subscription ID: $($sub.id)"
Write-Host "  Tenant:          $($sub.tenant)"
Write-Host "  Resource group:  $ResourceGroup"
Write-Host "  Account:         $accountName (FormRecognizer, S0) in $Location"
Write-Host '  Billing:         pay per analysed page; no monetary cap; check pricing and credits.'
Write-Host '  Data:            documents/images are sent to this Azure resource for OCR.'
Write-Host ''
if (-not $AssumeYes) {
    $answer = Read-Host "Create the account in subscription '$($sub.name)' ($($sub.id))? Type yes to continue"
    if ($answer -ne 'yes') { throw 'Aborted by user. Nothing was changed.' }
}

Write-Host "Creating $accountName..."
Invoke-Az 'create Document Intelligence account' cognitiveservices account create `
    --resource-group $ResourceGroup --name $accountName --kind FormRecognizer --sku S0 `
    --location $Location --custom-domain $accountName --yes --output none | Out-Null
$endpoint = Invoke-Az 'read account endpoint' cognitiveservices account show `
    --resource-group $ResourceGroup --name $accountName --query properties.endpoint --output tsv
if ($endpoint -notmatch '^https://\S+$') { throw 'Azure returned an unexpected endpoint.' }
Write-Host "Created. Endpoint: $endpoint"
Write-Host 'Next: run the "Azure ingestion job" workflow (or deploy/azure-ingest-job.ps1) so the job gets the endpoint and key.'
Write-Host "Delete (removes OCR only): az cognitiveservices account delete --resource-group $ResourceGroup --name $accountName --subscription $SubscriptionId"
