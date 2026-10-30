<#
.SYNOPSIS
    Adds idle auto-stop / on-demand start of the PostgreSQL server to an existing deployment.

.DESCRIPTION
    Container Apps already scale to zero and start on the first request. PostgreSQL does
    not, and it is the main cost. This script, for a deployment made by azure.ps1:
      * updates rag-api to the given image, enables its system-assigned managed identity,
        sets POSTGRES_RESOURCE_ID and lets the identity start the server (the API asks
        Azure to start a stopped server when it cannot connect);
      * creates the scheduled Container Apps Job 'rag-idle-stop' (managed identity) that
        stops the server when rag-api received no requests for -IdleMinutes.
    The managed identities get Contributor on the PostgreSQL server only, and the job
    Monitoring Reader on rag-api. Creating role assignments needs Owner or User Access
    Administrator (or Role Based Access Control Administrator) for the identity running this
    script; Contributor alone is not enough.

    Safe to re-run: existing identities, role assignments and the job are reused/updated.
    Remove again with: az containerapp job delete --name rag-idle-stop --resource-group <rg>
    (the server then simply stays running; also remove POSTGRES_RESOURCE_ID from rag-api).

.EXAMPLE
    pwsh ./deploy/azure-autostop.ps1 -SubscriptionId <guid> -Suffix ragdemo01 -Image ghcr.io/adranale/rag-showcase:<sha>
#>
#Requires -Version 7.0
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')][string]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{3,10}$')][string]$Suffix,
    [ValidatePattern('^[A-Za-z0-9._()-]{1,90}$')][string]$ResourceGroup = 'rg-rag-showcase',
    # Must be an image built from a commit that contains app/wake.py and idle_stop.py.
    [string]$Image = '',
    [ValidateRange(10, 1440)][int]$IdleMinutes = 30,
    # UTC. Every 10 minutes means the server stops 30-40 minutes after the last request.
    [string]$Schedule = '*/10 * * * *',
    [switch]$AssumeYes
)

$ErrorActionPreference = 'Stop'
$jobName = 'rag-idle-stop'

# Runs `az <args>`; $Label is a safe description used in errors (arguments are never printed).
function Invoke-Az {
    $label, $azArgs = $args
    if ($azArgs[0] -ne 'extension') { $azArgs += '--subscription', $SubscriptionId }
    $output = & az @azArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $lines = @($output | ForEach-Object { "$_" } | Where-Object { $_.Trim() })
        $detail = @($lines | Where-Object { $_ -match '^(ERROR|error:)|\(\w+\)' } | Select-Object -First 5) -join "`n"
        throw "Azure CLI step failed: $label (exit code $LASTEXITCODE).`n$detail"
    }
    ($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
        ForEach-Object { "$_" }) -join "`n" | ForEach-Object { $_.Trim() }
}

function Assert-Value([string]$Value, [string]$Name, [string]$Pattern = '\S') {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch $Pattern) {
        throw "Azure returned an empty or unexpected value for '$Name'. Stopping."
    }
    $Value
}

function Grant-Role([string]$PrincipalId, [string]$Role, [string]$Scope, [string]$Label) {
    $existing = Invoke-Az "check role $Label" role assignment list --assignee $PrincipalId --role $Role --scope $Scope --query '[].id' --output tsv
    if ($existing) { Write-Host "  role already present: $Label"; return }
    Invoke-Az "assign role $Label" role assignment create --assignee-object-id $PrincipalId `
        --assignee-principal-type ServicePrincipal --role $Role --scope $Scope --output none | Out-Null
    Write-Host "  role assigned: $Label"
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) is not installed.' }
Invoke-Az 'install/verify az containerapp extension' extension add --name containerapp --only-show-errors | Out-Null

$sub = Invoke-Az 'resolve subscription' account show --query '{name:name,id:id,tenant:tenantId,state:state}' --output json | ConvertFrom-Json
if ($sub.state -ne 'Enabled') { throw "Subscription $SubscriptionId is in state '$($sub.state)', not Enabled." }

if (-not $Image) {
    $sha = (& git -C (Split-Path $PSScriptRoot -Parent) rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $sha) { throw 'Pass -Image explicitly (git commit could not be determined).' }
    $Image = "ghcr.io/adranale/rag-showcase:$sha"
}
if ($Image -match ':latest$' -or $Image -notmatch '[:@]') {
    Write-Warning "Image '$Image' is a moving tag; prefer ghcr.io/adranale/rag-showcase:<commit-sha>."
}

$pgServer = "pg-rag-$Suffix"
$pgId = Assert-Value (Invoke-Az 'read PostgreSQL server' postgres flexible-server show `
        --resource-group $ResourceGroup --name $pgServer --query id --output tsv) 'PostgreSQL server ID' '^/subscriptions/'
$appId = Assert-Value (Invoke-Az 'read API app' containerapp show `
        --resource-group $ResourceGroup --name rag-api --query id --output tsv) 'API app ID' '^/subscriptions/'
$envId = Assert-Value (Invoke-Az 'read Container Apps environment' containerapp env show `
        --resource-group $ResourceGroup --name cae-rag-showcase --query id --output tsv) 'environment ID' '^/subscriptions/'

Write-Host ''
Write-Host "  Subscription:    $($sub.name) ($($sub.id)), tenant $($sub.tenant)"
Write-Host "  Resource group:  $ResourceGroup"
Write-Host "  PostgreSQL:      $pgServer (stopped after $IdleMinutes min without API requests, checked '$Schedule' UTC)"
Write-Host "  Image:           $Image (rag-api is restarted with it)"
Write-Host '  First request after a stop needs about 1-3 minutes while the database starts.'
Write-Host ''
if (-not $AssumeYes -and (Read-Host 'Apply? (yes/no)') -ne 'yes') { throw 'Cancelled.' }

Write-Host 'Updating rag-api (image, managed identity, POSTGRES_RESOURCE_ID)...'
Invoke-Az 'enable API managed identity' containerapp identity assign --resource-group $ResourceGroup --name rag-api --system-assigned --output none | Out-Null
$apiPrincipal = Assert-Value (Invoke-Az 'read API identity' containerapp show --resource-group $ResourceGroup --name rag-api `
        --query identity.principalId --output tsv) 'API identity' '^[0-9a-f-]{36}$'
Grant-Role $apiPrincipal 'Contributor' $pgId 'rag-api -> PostgreSQL server'
Invoke-Az 'update API app' containerapp update --resource-group $ResourceGroup --name rag-api `
    --image $Image --set-env-vars "POSTGRES_RESOURCE_ID=$pgId" --output none | Out-Null

Write-Host "Creating or updating job $jobName..."
$envVars = @("POSTGRES_RESOURCE_ID=$pgId", "API_APP_RESOURCE_ID=$appId", "IDLE_MINUTES=$IdleMinutes")
$jobExists = $true
try { Invoke-Az 'check idle-stop job' containerapp job show --resource-group $ResourceGroup --name $jobName --output none | Out-Null }
catch { $jobExists = $false }

if ($jobExists) {
    Invoke-Az 'update idle-stop job' containerapp job update --resource-group $ResourceGroup --name $jobName `
        --image $Image --cron-expression $Schedule --replace-env-vars @envVars --output none | Out-Null
}
else {
    Invoke-Az 'create idle-stop job' containerapp job create --resource-group $ResourceGroup --name $jobName `
        --environment $envId --trigger-type Schedule --cron-expression $Schedule `
        --replica-timeout 300 --replica-retry-limit 0 --replica-completion-count 1 --parallelism 1 `
        --image $Image --cpu 0.25 --memory 0.5Gi --command python --args idle_stop.py `
        --env-vars @envVars --mi-system-assigned --output none | Out-Null
}
$jobPrincipal = Assert-Value (Invoke-Az 'read job identity' containerapp job show --resource-group $ResourceGroup --name $jobName `
        --query identity.principalId --output tsv) 'job identity' '^[0-9a-f-]{36}$'
Grant-Role $jobPrincipal 'Contributor' $pgId 'job -> PostgreSQL server'
Grant-Role $jobPrincipal 'Monitoring Reader' $appId 'job -> rag-api metrics'

Write-Host ''
Write-Host 'Done. Role assignments can take a few minutes to take effect.'
Write-Host "Check the job:  az containerapp job execution list --name $jobName --resource-group $ResourceGroup --subscription $SubscriptionId --output table"
Write-Host "Server state:   az postgres flexible-server show --name $pgServer --resource-group $ResourceGroup --subscription $SubscriptionId --query state --output tsv"
Write-Host 'After a manual start (e.g. for ingestion) the server is stopped again 30-40 minutes later unless the API receives requests.'
