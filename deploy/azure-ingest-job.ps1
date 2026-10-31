<#
.SYNOPSIS
    Creates or updates the `rag-ingest` Azure Container Apps Job that ingests
    Dropbox / Google Drive documents inside Azure and embeds them.

.DESCRIPTION
    Runs in the Container Apps environment created by deploy/azure.ps1, with the
    SAME image as the deployed `rag-api` app (read from the app; the image must
    contain ingest_job.py, i.e. be built from a commit that includes it).
    The job executes `python ingest_job.py`, which runs init_db.py,
    ingest_remote.py and embed_chunks.py using PROVIDER / FOLDER from env vars.

    DATABASE_URL is never taken from a file or the repo: it is read from the
    existing `db-url` secret of the rag-api app and stored as a job secret.
    Provider credentials are read from environment variables of the caller
    (DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN, or
    GOOGLE_SERVICE_ACCOUNT_JSON, or GOOGLE_OAUTH_CLIENT_ID / _CLIENT_SECRET /
    _REFRESH_TOKEN) and stored as job secrets. Nothing is printed. Container Apps
    secrets are readable by anyone with Contributor access to the resource group.

    Safe to rerun: an existing job is updated in place. Changing the trigger type
    (Manual <-> Schedule) of an existing job requires deleting it first.

    The PostgreSQL server uses the "allow Azure services" rule, so no extra
    firewall rule is needed.

.EXAMPLE
    pwsh ./deploy/azure-ingest-job.ps1 -SubscriptionId <guid> -Suffix abc123 -Provider dropbox -Folder /Documents/rag -StartNow
    pwsh ./deploy/azure-ingest-job.ps1 -SubscriptionId <guid> -Suffix abc123 -Provider google_drive -Folder <folderId> -Schedule '0 3 * * *'
#>
#Requires -Version 7.0
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')][string]$SubscriptionId,
    [ValidatePattern('^[A-Za-z0-9._()-]{1,90}$')][string]$ResourceGroup = 'rg-rag-showcase',
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{3,10}$')][string]$Suffix,
    [Parameter(Mandatory)][ValidateSet('dropbox', 'google_drive', 'gdrive')][string]$Provider,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Folder,
    # Cron expression (5 fields, UTC). Empty = manual trigger only.
    [string]$Schedule = '',
    # Must equal the image of the deployed rag-api app; default: read from the app.
    [string]$Image = '',
    [string]$EnvironmentName = 'cae-rag-showcase',
    [ValidateRange(60, 86400)][int]$TimeoutSeconds = 3600,
    [ValidateRange(0, 1)][int]$RetryLimit = 0,
    [switch]$DeleteRemoved,
    [switch]$StartNow,
    [switch]$AssumeYes
)

$ErrorActionPreference = 'Stop'
$script:SecretValues = [System.Collections.Generic.List[string]]::new()
$jobName = 'rag-ingest'
if ($Provider -eq 'gdrive') { $Provider = 'google_drive' }

function Protect-Text([string]$Text) {
    foreach ($secret in $script:SecretValues) {
        if ($secret) { $Text = $Text.Replace($secret, '***') }
    }
    $Text
}

# Runs `az <args>`. $Label is a safe description used in errors; the raw arguments
# (which may hold secrets) are never printed. Returns trimmed stdout.
function Invoke-Az {
    $label, $azArgs = $args
    if ($azArgs[0] -notin 'extension', 'version', 'rest' -and 'list-locations' -notin $azArgs) {
        $azArgs += '--subscription', $SubscriptionId
    }
    $output = & az @azArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $lines = @($output | ForEach-Object { "$_" } | Where-Object { $_.Trim() })
        $errorLines = @($lines | Where-Object { $_ -match '^(ERROR|error:|usage:)|unrecognized|invalid|\(\w+\)' })
        $detail = (@($errorLines | Select-Object -First 6) + @($lines | Select-Object -First 2) | Select-Object -Unique) -join "`n"
        throw "Azure CLI step failed: $label (exit code $LASTEXITCODE).`n$(Protect-Text $detail)"
    }
    ($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
        ForEach-Object { "$_" }) -join "`n" | ForEach-Object { $_.Trim() }
}

function Assert-Value([string]$Value, [string]$Name, [string]$Pattern = '\S') {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -notmatch $Pattern) {
        throw "Azure returned an empty or unexpected value for '$Name'. Stopping before using it."
    }
    $Value
}

function Get-EnvValue([string]$Name) {
    [Environment]::GetEnvironmentVariable($Name)
}

Write-Host '== Preflight (nothing is changed yet) =='
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI (az) is not installed. macOS: brew install azure-cli'
}
if ($Schedule -and $Schedule.Trim() -notmatch '^\S+(\s+\S+){4}$') {
    throw "-Schedule must be a 5-field cron expression (UTC), e.g. '0 3 * * *'."
}
$triggerType = if ($Schedule) { 'Schedule' } else { 'Manual' }

# Provider credentials: environment variable name -> job secret name.
$credentialMap = [ordered]@{}
if ($Provider -eq 'dropbox') {
    $required = 'DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN'
    $missing = @($required | Where-Object { -not (Get-EnvValue $_) })
    if ($missing) {
        throw "Missing environment variables for Dropbox: $($missing -join ', '). Set them in the calling environment (they are never printed)."
    }
    foreach ($name in $required) { $credentialMap[$name] = (Get-EnvValue $name) }
}
else {
    $serviceAccount = Get-EnvValue 'GOOGLE_SERVICE_ACCOUNT_JSON'
    if ($serviceAccount) {
        try { $null = $serviceAccount | ConvertFrom-Json -ErrorAction Stop }
        catch { throw 'GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.' }
        # Base64 keeps the JSON quoting out of the az command line.
        $credentialMap['GOOGLE_SERVICE_ACCOUNT_JSON_B64'] = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($serviceAccount))
    }
    else {
        $required = 'GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET', 'GOOGLE_OAUTH_REFRESH_TOKEN'
        $missing = @($required | Where-Object { -not (Get-EnvValue $_) })
        if ($missing) {
            throw "Missing Google Drive credentials: set GOOGLE_SERVICE_ACCOUNT_JSON, or all of: $($required -join ', ')."
        }
        foreach ($name in $required) { $credentialMap[$name] = (Get-EnvValue $name) }
    }
}
foreach ($value in $credentialMap.Values) { $script:SecretValues.Add([string]$value) }
if ($serviceAccount) { $script:SecretValues.Add($serviceAccount) }

Invoke-Az 'install/verify az containerapp extension' extension add --name containerapp --only-show-errors | Out-Null

try {
    $sub = Invoke-Az 'resolve subscription' account show --query '{name:name,id:id,tenant:tenantId,state:state}' --output json | ConvertFrom-Json
}
catch {
    throw "Subscription $SubscriptionId is not accessible with the current 'az login'. Run 'az account list --output table' to see available subscriptions."
}
if ($sub.state -ne 'Enabled') { throw "Subscription $SubscriptionId is in state '$($sub.state)', not Enabled." }

Assert-Value (Invoke-Az 'check Container Apps environment' containerapp env show `
        --resource-group $ResourceGroup --name $EnvironmentName --query name --output tsv) 'Container Apps environment' | Out-Null

# The job must run the image that rag-api runs (same schema/code version).
$apiImage = Assert-Value (Invoke-Az 'read rag-api image' containerapp show `
        --resource-group $ResourceGroup --name rag-api `
        --query 'properties.template.containers[0].image' --output tsv) 'rag-api image'
if (-not $Image) { $Image = $apiImage }
elseif ($Image -ne $apiImage) {
    throw "-Image '$Image' differs from the deployed rag-api image '$apiImage'. Update rag-api to the same image first (the job must match the API/schema version)."
}
if ($Image -match ':latest$') {
    Write-Warning "Image '$Image' is a moving tag; the job would pick up whatever 'latest' is at each run."
}

$databaseUrl = Assert-Value (Invoke-Az 'read rag-api db-url secret' containerapp secret show `
        --resource-group $ResourceGroup --name rag-api --secret-name db-url `
        --query value --output tsv) 'DATABASE_URL secret' '^postgresql://\S+$'
$script:SecretValues.Add($databaseUrl)

$existing = Invoke-Az 'check for existing job' containerapp job list `
    --resource-group $ResourceGroup --query "[?name=='$jobName'].name" --output tsv
$jobExists = $existing -eq $jobName
if ($jobExists) {
    $currentTrigger = Assert-Value (Invoke-Az 'read job trigger type' containerapp job show `
            --resource-group $ResourceGroup --name $jobName `
            --query properties.configuration.triggerType --output tsv) 'job trigger type'
    if ($currentTrigger -ne $triggerType) {
        throw "Job '$jobName' has trigger type $currentTrigger; it cannot be changed to $triggerType in place. Delete it first: az containerapp job delete --resource-group $ResourceGroup --name $jobName --subscription $SubscriptionId --yes"
    }
}

Write-Host ''
Write-Host "  Subscription:    $($sub.name)"
Write-Host "  Subscription ID: $($sub.id)"
Write-Host "  Tenant:          $($sub.tenant)"
Write-Host "  Resource group:  $ResourceGroup"
Write-Host "  Environment:     $EnvironmentName"
Write-Host "  Job:             $jobName ($(if ($jobExists) { 'update' } else { 'create' }), trigger $triggerType$(if ($Schedule) { " '$Schedule' UTC" }))"
Write-Host "  Image:           $Image"
Write-Host "  Provider/folder: $Provider  $Folder"
Write-Host '  Credentials:     found in environment (not printed)'
Write-Host ''
if (-not $AssumeYes) {
    $answer = Read-Host "Create/update the job in subscription '$($sub.name)' ($($sub.id))? Type yes to continue"
    if ($answer -ne 'yes') { throw 'Aborted by user. Nothing was changed.' }
}

# Secret names are lowercase with hyphens; env vars reference them via secretref.
$secretArgs = @("db-url=$databaseUrl")
$envArgs = @("PROVIDER=$Provider", "FOLDER=$Folder", 'DATABASE_URL=secretref:db-url')
if ($DeleteRemoved) { $envArgs += 'DELETE_REMOVED=true' }
$secretNames = @('db-url')
foreach ($name in $credentialMap.Keys) {
    $secretName = $name.ToLowerInvariant().Replace('_', '-')
    $secretNames += $secretName
    $secretArgs += "$secretName=$($credentialMap[$name])"
    $envArgs += "$name=secretref:$secretName"
}

# `--command python --args ingest_job.py`: no value starts with "-", which az
# would parse as an option; everything else is configured through env vars.
if ($jobExists) {
    Write-Host "Updating job $jobName..."
    Invoke-Az 'set job secrets' containerapp job secret set `
        --resource-group $ResourceGroup --name $jobName --secrets @secretArgs --output none | Out-Null
    $stale = @((Invoke-Az 'list job secrets' containerapp job secret list `
                --resource-group $ResourceGroup --name $jobName --query '[].name' --output tsv) -split "`n" |
            Where-Object { $_ -and $_ -notin $secretNames })
    if ($stale) {
        Invoke-Az 'remove unused job secrets' containerapp job secret remove `
            --resource-group $ResourceGroup --name $jobName --secret-names @stale --yes --output none | Out-Null
    }
    $updateArgs = @(
        '--resource-group', $ResourceGroup, '--name', $jobName, '--image', $Image,
        '--cpu', '1', '--memory', '2Gi',
        '--replica-timeout', "$TimeoutSeconds", '--replica-retry-limit', "$RetryLimit",
        '--command', 'python', '--args', 'ingest_job.py', '--output', 'none')
    if ($Schedule) { $updateArgs += '--cron-expression', $Schedule.Trim() }
    Invoke-Az 'update ingestion job' containerapp job update @updateArgs --replace-env-vars @envArgs | Out-Null
}
else {
    Write-Host "Creating job $jobName..."
    $createArgs = @(
        '--resource-group', $ResourceGroup, '--environment', $EnvironmentName, '--name', $jobName,
        '--image', $Image, '--trigger-type', $triggerType,
        '--replica-timeout', "$TimeoutSeconds", '--replica-retry-limit', "$RetryLimit",
        '--replica-completion-count', '1', '--parallelism', '1',
        '--cpu', '1', '--memory', '2Gi',
        '--command', 'python', '--args', 'ingest_job.py', '--output', 'none')
    if ($Schedule) { $createArgs += '--cron-expression', $Schedule.Trim() }
    Invoke-Az 'create ingestion job' containerapp job create @createArgs `
        --secrets @secretArgs --env-vars @envArgs | Out-Null
}
Write-Host "Job '$jobName' is configured."

if ($StartNow) {
    $execution = Assert-Value (Invoke-Az 'start job execution' containerapp job start `
            --resource-group $ResourceGroup --name $jobName --query name --output tsv) 'job execution name'
    Write-Host "Started execution: $execution"
}
Write-Host ''
Write-Host 'Executions and status:'
Write-Host "  az containerapp job execution list --resource-group $ResourceGroup --name $jobName --subscription $SubscriptionId --output table"
Write-Host 'Logs: Log Analytics workspace of the environment, table ContainerAppConsoleLogs_CL (ContainerGroupName_s starts with the execution name).'
Write-Host 'The job bills only while an execution runs; Log Analytics ingestion and PostgreSQL are billed separately.'
Write-Host "Delete: az containerapp job delete --resource-group $ResourceGroup --name $jobName --subscription $SubscriptionId --yes"
