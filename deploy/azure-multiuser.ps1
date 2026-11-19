<#
.SYNOPSIS
    Upgrades an existing RAG Showcase Azure deployment to multi-user mode (v2).

.DESCRIPTION
    Run AFTER deploy/azure.ps1 (infrastructure, rag-api, rag-ui exist). In ONE explicitly
    chosen subscription it:
      1. creates the manual Container Apps Jobs rag-migrate (schema + runtime role),
         rag-sync (per-user Dropbox sync worker) and rag-admin (manage_users.py);
      2. optionally runs the migration (-RunMigration): applies sql/*.sql with the admin
         connection, sets the rag_runtime password and, with OWNER_EMAIL, assigns the
         existing documents to that user;
      3. reconfigures rag-api (connects as rag_runtime, user-token secret, Dropbox app,
         token encryption key, worker job id, system identity + 'Container Apps Jobs
         Operator' on rag-sync) and rag-ui (Microsoft login through `python ui_start.py`,
         APP_PASSWORD removed);
      4. with -EmailNotifications: creates/reuses Azure Communication Services
         (acs-rag-<suffix>) with an Azure-managed e-mail domain (acs-email-<suffix>) and
         gives rag-api its connection string, so admins get an e-mail per access request.
         Needs the Microsoft.Communication provider registered once by a subscription owner.

    The existing `db-url` secret (admin) is left unchanged for the migrate/admin jobs and the
    legacy workflows; the API uses the new `db-runtime-url` secret.

    Generated secrets (USER_TOKEN_SECRET, TOKEN_ENCRYPTION_KEY, AUTH_COOKIE_SECRET,
    RUNTIME_DB_PASSWORD) are taken from the env file when present, else from the existing
    Azure app secrets, else generated; new values are saved to the git-ignored env file
    BEFORE anything is changed and are never printed. Rerunning is safe and never rotates
    them (rotating TOKEN_ENCRYPTION_KEY would make stored Dropbox tokens unreadable).

    Caller environment (never printed): MS_CLIENT_ID, MS_CLIENT_SECRET, DROPBOX_APP_KEY,
    DROPBOX_APP_SECRET; optional OWNER_EMAIL, MS_TENANT (default consumers),
    DOCINTEL_ENDPOINT/DOCINTEL_KEY/DOCINTEL_MAX_PAGES (OCR for the worker).

    Not production-hardened: Container Apps secrets are readable by anyone with Contributor
    access to the resource group, and the admin connection string stays in the rag-api
    secrets (the app code does not use it). See README "Multi-user mode (v2)".

.EXAMPLE
    pwsh ./deploy/azure-multiuser.ps1 -SubscriptionId <guid> -Suffix abc123 -Image ghcr.io/adranale/rag-showcase:<commit-sha> -PreflightOnly
    pwsh ./deploy/azure-multiuser.ps1 -SubscriptionId <guid> -Suffix abc123 -Image ghcr.io/adranale/rag-showcase:<commit-sha> -RunMigration
#>
#Requires -Version 7.0
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')][string]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{3,10}$')][string]$Suffix,
    # Immutable tag (commit SHA) of an image built from a commit that contains v2.
    [Parameter(Mandatory)][ValidatePattern('^ghcr\.io/[a-z0-9._/-]+(:[\w.-]+|@sha256:[a-f0-9]{64})$')][string]$Image,
    [ValidatePattern('^[A-Za-z0-9._()-]{1,90}$')][string]$ResourceGroup = 'rg-rag-showcase',
    [string]$EnvironmentName = 'cae-rag-showcase',
    # Where generated secrets are kept (default: .env.azure.v2 in the repo root, git-ignored).
    [string]$EnvFile = '',
    [switch]$PreflightOnly,
    [switch]$RunMigration,
    # Create/reuse Azure Communication Services (Azure-managed e-mail domain) and let the
    # API e-mail the admins about new access requests.
    [switch]$EmailNotifications,
    [switch]$AssumeYes
)

$ErrorActionPreference = 'Stop'
$script:SecretValues = [System.Collections.Generic.List[string]]::new()
$envFile = if ($EnvFile) { $EnvFile } else { Join-Path (Split-Path $PSScriptRoot -Parent) '.env.azure.v2' }
$pgServer = "pg-rag-$Suffix"
$roleName = 'Container Apps Jobs Operator'
$emailServiceName = "acs-email-$Suffix"
$communicationName = "acs-rag-$Suffix"
$acsApiVersion = '2023-04-01'

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

function New-Secret([int]$Length = 32) {
    $chars = [char[]]'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    -join (1..$Length | ForEach-Object {
            $chars[[System.Security.Cryptography.RandomNumberGenerator]::GetInt32($chars.Length)]
        })
}

# A Fernet key: 32 random bytes, URL-safe base64.
function New-FernetKey {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
}

# Value of an app secret, or $null when the app or the secret does not exist.
function Get-AppSecret([string]$App, [string]$Name) {
    try {
        $value = Invoke-Az "read $Name secret of $App" containerapp secret show `
            --resource-group $ResourceGroup --name $App --secret-name $Name --query value --output tsv
        if ($value) { $value } else { $null }
    }
    catch {
        if ("$_" -match 'NotFound|not found|could not be found|does not exist|ResourceNotFound|SecretNotFound|does not have a secret') { $null } else { throw }
    }
}

function Test-Exists {
    $label, $azArgs = $args
    try { Invoke-Az $label @azArgs --output none | Out-Null; $true }
    catch {
        if ("$_" -match 'NotFound|not found|could not be found|does not exist|ResourceNotFound') { $false } else { throw }
    }
}

function Get-EnvValue([string]$Name) { [Environment]::GetEnvironmentVariable($Name) }

Write-Host '== Preflight (nothing is changed yet) =='
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI (az) is not installed. macOS: brew install azure-cli'
}
$required = 'MS_CLIENT_ID', 'MS_CLIENT_SECRET', 'DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET'
$missing = @($required | Where-Object { -not (Get-EnvValue $_) })
if ($missing) {
    throw "Missing environment variables: $($missing -join ', '). Set them in the calling environment (they are never printed)."
}
foreach ($name in 'MS_CLIENT_SECRET', 'DROPBOX_APP_SECRET', 'DOCINTEL_KEY') {
    if (Get-EnvValue $name) { $script:SecretValues.Add([string](Get-EnvValue $name)) }
}
$msTenant = if (Get-EnvValue 'MS_TENANT') { Get-EnvValue 'MS_TENANT' } else { 'consumers' }
if ($msTenant -ne 'consumers') {
    Write-Warning "MS_TENANT='$msTenant': Streamlit login fails with invalid_claim: iss for common/organizations (see README). Use 'consumers' unless you verified a fix."
}
$ownerEmail = Get-EnvValue 'OWNER_EMAIL'
if ($ownerEmail -and $ownerEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { throw 'OWNER_EMAIL is not a valid e-mail address.' }
if ($RunMigration -and -not $ownerEmail) {
    Write-Warning 'OWNER_EMAIL is not set: existing documents will stay unowned (invisible to every user).'
}
$docIntel = @('DOCINTEL_ENDPOINT', 'DOCINTEL_KEY') | Where-Object { Get-EnvValue $_ }
if ($docIntel -and $docIntel.Count -ne 2) { throw 'Set both DOCINTEL_ENDPOINT and DOCINTEL_KEY, or neither.' }
if ($docIntel -and (Get-EnvValue 'DOCINTEL_ENDPOINT') -notmatch '^https://') { throw 'DOCINTEL_ENDPOINT must be an https:// URL.' }
$maxPages = Get-EnvValue 'DOCINTEL_MAX_PAGES'
if ($maxPages -and $maxPages -notmatch '^\d+$') { throw 'DOCINTEL_MAX_PAGES must be a non-negative integer.' }
if ($Image -match ':latest$') { Write-Warning "Image '$Image' is a moving tag. Prefer the commit SHA built by CI." }

Invoke-Az 'install/verify az containerapp extension' extension add --name containerapp --only-show-errors | Out-Null
try {
    $sub = Invoke-Az 'resolve subscription' account show --query '{name:name,id:id,tenant:tenantId,state:state}' --output json | ConvertFrom-Json
}
catch {
    throw "Subscription $SubscriptionId is not accessible with the current 'az login'. Run 'az account list --output table'."
}
if ($sub.state -ne 'Enabled') { throw "Subscription $SubscriptionId is in state '$($sub.state)', not Enabled." }
if ($EmailNotifications) {
    $acsProvider = Invoke-Az 'check Microsoft.Communication provider' provider show --namespace Microsoft.Communication --query registrationState --output tsv
    if ($acsProvider -ne 'Registered') {
        throw "Resource provider Microsoft.Communication is '$acsProvider'. A subscription owner must register it once: az provider register --namespace Microsoft.Communication --subscription $SubscriptionId --wait"
    }
}

Assert-Value (Invoke-Az 'check Container Apps environment' containerapp env show `
        --resource-group $ResourceGroup --name $EnvironmentName --query name --output tsv) 'Container Apps environment' | Out-Null
$adminUrl = Assert-Value (Get-AppSecret 'rag-api' 'db-url') 'rag-api db-url secret (is the base deployment complete?)' "^postgresql://ragadmin:[^@]+@${pgServer}\.postgres\.database\.azure\.com:5432/rag\?sslmode=require$"
$script:SecretValues.Add($adminUrl)
$apiKey = Assert-Value (Get-AppSecret 'rag-ui' 'api-key') 'rag-ui api-key secret'
$uiFqdn = Assert-Value (Invoke-Az 'read UI FQDN' containerapp show --resource-group $ResourceGroup --name rag-ui `
        --query properties.configuration.ingress.fqdn --output tsv) 'UI FQDN' '^[a-z0-9.-]+\.azurecontainerapps\.io$'

# --- Generated secrets: env file, else existing Azure secrets, else new --------
$known = @{}
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object { $k, $v = $_ -split '=', 2; if ($v) { $known[$k] = $v } }
}
$existingRuntimeUrl = Get-AppSecret 'rag-api' 'db-runtime-url'
$existingRuntimePassword = if ($existingRuntimeUrl -match '^postgresql://rag_runtime:([^@]+)@') { $Matches[1] } else { $null }
$sources = [ordered]@{
    USER_TOKEN_SECRET    = { Get-AppSecret 'rag-api' 'user-token-secret' }
    TOKEN_ENCRYPTION_KEY = { Get-AppSecret 'rag-api' 'token-encryption-key' }
    AUTH_COOKIE_SECRET   = { Get-AppSecret 'rag-ui' 'auth-cookie-secret' }
    RUNTIME_DB_PASSWORD  = { $existingRuntimePassword }
}
$generators = @{
    USER_TOKEN_SECRET    = { New-Secret 48 }
    TOKEN_ENCRYPTION_KEY = { New-FernetKey }
    AUTH_COOKIE_SECRET   = { New-Secret 48 }
    RUNTIME_DB_PASSWORD  = { New-Secret 32 }
}
$generated = @{}
$newKeys = @()
foreach ($name in $sources.Keys) {
    $value = $known[$name]
    if (-not $value) { $value = & $sources[$name] }
    if (-not $value) { $value = & $generators[$name]; $newKeys += $name }
    elseif (-not $known[$name]) { $newKeys += $name }
    $generated[$name] = $value
    $script:SecretValues.Add($value)
}
$secretNote = if ($newKeys) { "not in the env file yet (reused from Azure when present, else generated): $($newKeys -join ', ')" } else { 'all found' }

$runtimeUrl = $adminUrl -replace '^postgresql://ragadmin:[^@]+@', "postgresql://rag_runtime:$($generated['RUNTIME_DB_PASSWORD'])@"
$script:SecretValues.Add($runtimeUrl)

Write-Host ''
Write-Host "  Subscription:    $($sub.name)"
Write-Host "  Subscription ID: $($sub.id)"
Write-Host "  Tenant:          $($sub.tenant)"
Write-Host "  Resource group:  $ResourceGroup  (environment $EnvironmentName)"
Write-Host "  Image:           $Image"
Write-Host "  UI:              https://$uiFqdn   (login redirect https://$uiFqdn/oauth2callback)"
Write-Host "  Microsoft tenant: $msTenant"
Write-Host "  Generated secrets: $secretNote"
Write-Host "  Migration:       $(if ($RunMigration) { "will run now$(if ($ownerEmail) { ' (existing data -> owner)' })" } else { 'not run (use -RunMigration, or run job rag-migrate yourself) - run it BEFORE users log in' })"
Write-Host "  OCR for worker:  $(if ($docIntel) { 'enabled' } else { 'disabled' })"
Write-Host "  E-mail to admins: $(if ($EmailNotifications) { "enabled ($communicationName / $emailServiceName, Azure-managed domain, data in Europe; charged per e-mail)" } else { 'not configured by this run' })"
Write-Host ''
Write-Host 'Manual checks: Entra app redirect URI, Dropbox app redirect URI (the UI root) and scopes,'
Write-Host '  Dropbox development-mode user limit. See README "Multi-user mode (v2)".'
if ($PreflightOnly) { Write-Host 'Preflight passed. Nothing was changed.' -ForegroundColor Green; return }
if (-not $AssumeYes) {
    $answer = Read-Host "Apply to subscription '$($sub.name)' ($($sub.id))? Type yes to continue"
    if ($answer -ne 'yes') { throw 'Aborted by user. Nothing was changed.' }
}

# Persist generated secrets first (never overwrites; only appends missing keys).
if ($newKeys) {
    if (-not (Test-Path $envFile)) {
        New-Item -ItemType File -Path $envFile -ErrorAction Stop | Out-Null
        if ($IsWindows) { Write-Warning "$envFile permissions are not restricted on Windows; protect the file yourself." }
        else {
            & chmod 600 $envFile
            if ($LASTEXITCODE -ne 0) { throw "Could not restrict $envFile permissions; stopping." }
        }
    }
    Add-Content -Path $envFile -Value @($newKeys | Where-Object { -not $known[$_] } | ForEach-Object { "$_=$($generated[$_])" })
    Write-Host "Generated secrets saved to $envFile (git-ignored)."
}

# --- Jobs --------------------------------------------------------------------
function Set-Job([string]$Name, [string[]]$Command, [string[]]$Secrets, [string[]]$EnvVars, [string]$Memory = '2Gi') {
    $exists = Test-Exists "check job $Name" containerapp job show --resource-group $ResourceGroup --name $Name
    $commandArgs = @($Command[1..($Command.Count - 1)])
    # Consumption profile only allows fixed CPU/memory pairs (0.5/1Gi, 1/2Gi).
    $cpu = if ($Memory -eq '1Gi') { '0.5' } else { '1' }
    if ($exists) {
        Write-Host "Updating job $Name..."
        Invoke-Az "set secrets of job $Name" containerapp job secret set --resource-group $ResourceGroup --name $Name --secrets @Secrets --output none | Out-Null
        Invoke-Az "update job $Name" containerapp job update --resource-group $ResourceGroup --name $Name --image $Image `
            --cpu $cpu --memory $Memory --replica-timeout 3600 --replica-retry-limit 0 `
            --command $Command[0] --args @commandArgs --replace-env-vars @EnvVars --output none | Out-Null
    }
    else {
        Write-Host "Creating job $Name..."
        Invoke-Az "create job $Name" containerapp job create --resource-group $ResourceGroup --environment $EnvironmentName --name $Name `
            --image $Image --trigger-type Manual --replica-timeout 3600 --replica-retry-limit 0 `
            --replica-completion-count 1 --parallelism 1 --cpu $cpu --memory $Memory `
            --command $Command[0] --args @commandArgs --secrets @Secrets --env-vars @EnvVars --output none | Out-Null
    }
}

$migrateEnv = @('ADMIN_DATABASE_URL=secretref:db-url', 'RUNTIME_DB_PASSWORD=secretref:runtime-db-password')
if ($ownerEmail) { $migrateEnv += "OWNER_EMAIL=$ownerEmail" }
Set-Job 'rag-migrate' @('python', 'migrate_job.py') @("db-url=$adminUrl", "runtime-db-password=$($generated['RUNTIME_DB_PASSWORD'])") $migrateEnv '1Gi'

# User administration: start with e.g. --command python --args manage_users.py invite <email>
Set-Job 'rag-admin' @('python', 'manage_users.py', 'list') @("db-url=$adminUrl") @('DATABASE_URL=secretref:db-url') '1Gi'

$workerSecrets = @("db-runtime-url=$runtimeUrl", "token-encryption-key=$($generated['TOKEN_ENCRYPTION_KEY'])", "dropbox-app-secret=$(Get-EnvValue 'DROPBOX_APP_SECRET')")
$workerEnv = @('DATABASE_URL=secretref:db-runtime-url', 'TOKEN_ENCRYPTION_KEY=secretref:token-encryption-key',
    "DROPBOX_APP_KEY=$(Get-EnvValue 'DROPBOX_APP_KEY')", 'DROPBOX_APP_SECRET=secretref:dropbox-app-secret')
if ($docIntel) {
    $workerSecrets += "docintel-key=$(Get-EnvValue 'DOCINTEL_KEY')"
    $workerEnv += "DOCINTEL_ENDPOINT=$(Get-EnvValue 'DOCINTEL_ENDPOINT')", 'DOCINTEL_KEY=secretref:docintel-key'
    if ($maxPages) { $workerEnv += "DOCINTEL_MAX_PAGES=$maxPages" }
}
Set-Job 'rag-sync' @('python', 'sync_worker.py') $workerSecrets $workerEnv
$syncJobId = Assert-Value (Invoke-Az 'read rag-sync resource id' containerapp job show --resource-group $ResourceGroup --name rag-sync --query id --output tsv) 'rag-sync id' '^/subscriptions/'

# --- Migration -----------------------------------------------------------------
if ($RunMigration) {
    $pgState = Invoke-Az 'read PostgreSQL state' postgres flexible-server show --resource-group $ResourceGroup --name $pgServer --query state --output tsv
    if ($pgState -eq 'Stopped') {
        Write-Host 'PostgreSQL server is stopped; starting it...'
        Invoke-Az 'start PostgreSQL server' postgres flexible-server start --resource-group $ResourceGroup --name $pgServer --output none | Out-Null
    }
    $execution = Assert-Value (Invoke-Az 'start migration job' containerapp job start --resource-group $ResourceGroup --name rag-migrate --query name --output tsv) 'migration execution name'
    Write-Host "Migration started ($execution); waiting..."
    $status = ''
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 15
        $status = Invoke-Az 'read migration status' containerapp job execution list --resource-group $ResourceGroup --name rag-migrate `
            --query "[?name=='$execution'].properties.status | [0]" --output tsv
        if ($status -in 'Succeeded', 'Failed', 'Stopped', 'Degraded') { break }
    }
    if ($status -ne 'Succeeded') {
        throw "Migration did not succeed (status '$status'). The apps were NOT switched. Logs: Log Analytics ContainerAppConsoleLogs_CL (execution $execution). Fix and rerun."
    }
    Write-Host 'Migration succeeded.' -ForegroundColor Green
}
else {
    Write-Warning 'Migration was not run here. Apps are switched anyway; run job rag-migrate before the first login or the API will fail (missing rag_runtime role / tables).'
}

# --- E-mail notifications (optional) ---------------------------------------------
# PUT the ARM resource unless it already exists in the wanted state, then wait until it
# is provisioned. ARM REST through `az rest` avoids the az communication extension.
function Set-ArmResource([string]$Label, [string]$Id, [hashtable]$Body, [scriptblock]$IsCurrent = { $true }) {
    $url = "https://management.azure.com${Id}?api-version=$acsApiVersion"
    $resource = $null
    try { $resource = Invoke-Az "read $Label" rest --method get --url $url --output json | ConvertFrom-Json }
    catch { if ("$_" -notmatch 'NotFound|ResourceNotFound|could not be found|\b404\b') { throw } }
    if ($resource -and $resource.properties.provisioningState -eq 'Succeeded' -and (& $IsCurrent $resource)) {
        return $resource
    }
    Write-Host "Creating/updating $Label..."
    $file = New-TemporaryFile
    try {
        [System.IO.File]::WriteAllText($file.FullName, ($Body | ConvertTo-Json -Depth 10))
        Invoke-Az "create $Label" rest --method put --url $url --body "@$file" --output none | Out-Null
    }
    finally { Remove-Item $file -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 60; $i++) {
        $resource = Invoke-Az "read $Label" rest --method get --url $url --output json | ConvertFrom-Json
        $state = $resource.properties.provisioningState
        if ($state -eq 'Succeeded') { return $resource }
        if ($state -in 'Failed', 'Canceled') { throw "$Label provisioning ended in state '$state'." }
        Start-Sleep -Seconds 10
    }
    throw "$Label was not provisioned within 10 minutes; rerun the script."
}

$apiEmailSecrets = @()
$apiEmailEnv = @()
if ($EmailNotifications) {
    $rgId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
    $emailServiceId = "$rgId/providers/Microsoft.Communication/emailServices/$emailServiceName"
    Set-ArmResource "e-mail service $emailServiceName" $emailServiceId @{
        location = 'global'; properties = @{ dataLocation = 'Europe' }
    } | Out-Null
    $domainId = "$emailServiceId/domains/AzureManagedDomain"
    $domain = Set-ArmResource 'Azure-managed e-mail domain' $domainId @{
        location   = 'global'
        properties = @{ domainManagement = 'AzureManaged'; userEngagementTracking = 'Disabled' }
    }
    $senderDomain = Assert-Value $domain.properties.mailFromSenderDomain 'e-mail sender domain' '^[a-z0-9.-]+\.azurecomm\.net$'
    $communicationId = "$rgId/providers/Microsoft.Communication/communicationServices/$communicationName"
    Set-ArmResource "Communication Services $communicationName" $communicationId @{
        location = 'global'; properties = @{ dataLocation = 'Europe'; linkedDomains = @($domainId) }
    } { param($r) @($r.properties.linkedDomains) -contains $domainId } | Out-Null
    $acsConnection = Assert-Value (Invoke-Az 'read Communication Services key' rest --method post `
            --url "https://management.azure.com$communicationId/listKeys?api-version=$acsApiVersion" `
            --query primaryConnectionString --output tsv) 'Communication Services connection string' '^endpoint=https://'
    $script:SecretValues.Add($acsConnection)
    $apiEmailSecrets = @("acs-email-connection=$acsConnection")
    $apiEmailEnv = @('ACS_EMAIL_CONNECTION_STRING=secretref:acs-email-connection', "ACS_EMAIL_SENDER=DoNotReply@$senderDomain")
    Write-Host "E-mail notifications ready (sender DoNotReply@$senderDomain)."
}

# --- rag-api -------------------------------------------------------------------
Write-Host 'Configuring rag-api...'
$principalId = Assert-Value (Invoke-Az 'enable system identity on rag-api' containerapp identity assign `
        --resource-group $ResourceGroup --name rag-api --system-assigned --query principalId --output tsv) 'rag-api principal id' '^[0-9a-fA-F-]{36}$'
Invoke-Az 'set rag-api secrets' containerapp secret set --resource-group $ResourceGroup --name rag-api --secrets `
    "db-runtime-url=$runtimeUrl" "user-token-secret=$($generated['USER_TOKEN_SECRET'])" `
    "token-encryption-key=$($generated['TOKEN_ENCRYPTION_KEY'])" "dropbox-app-secret=$(Get-EnvValue 'DROPBOX_APP_SECRET')" `
    @apiEmailSecrets --output none | Out-Null
Invoke-Az 'update rag-api' containerapp update --resource-group $ResourceGroup --name rag-api --image $Image --set-env-vars `
    DATABASE_URL=secretref:db-runtime-url USER_TOKEN_SECRET=secretref:user-token-secret `
    TOKEN_ENCRYPTION_KEY=secretref:token-encryption-key DROPBOX_APP_SECRET=secretref:dropbox-app-secret `
    "DROPBOX_APP_KEY=$(Get-EnvValue 'DROPBOX_APP_KEY')" "DROPBOX_REDIRECT_URI=https://$uiFqdn/" `
    "UI_BASE_URL=https://$uiFqdn/" "WORKER_JOB_RESOURCE_ID=$syncJobId" @apiEmailEnv --output none | Out-Null

try {
    $hasRole = Invoke-Az 'check worker role assignment' role assignment list --assignee $principalId --scope $syncJobId `
        --query "[?roleDefinitionName=='$roleName'] | length(@)" --output tsv
    if ($hasRole -eq '0') {
        Invoke-Az 'grant worker-start role to rag-api' role assignment create --assignee-object-id $principalId `
            --assignee-principal-type ServicePrincipal --role $roleName --scope $syncJobId --output none | Out-Null
        Write-Host "Granted '$roleName' on rag-sync to the rag-api identity."
    }
}
catch {
    Write-Warning "Could not verify/assign '$roleName' (needs Owner/User Access Administrator): $_`nWithout it /sync only queues the job; run rag-sync manually or assign the role: az role assignment create --assignee-object-id $principalId --assignee-principal-type ServicePrincipal --role '$roleName' --scope $syncJobId --subscription $SubscriptionId"
}

# --- rag-ui --------------------------------------------------------------------
Write-Host 'Configuring rag-ui...'
Invoke-Az 'set rag-ui secrets' containerapp secret set --resource-group $ResourceGroup --name rag-ui --secrets `
    "user-token-secret=$($generated['USER_TOKEN_SECRET'])" "auth-cookie-secret=$($generated['AUTH_COOKIE_SECRET'])" `
    "ms-client-secret=$(Get-EnvValue 'MS_CLIENT_SECRET')" --output none | Out-Null
# `--args ui_start.py` does not start with "-", so az parses it reliably.
$uiEnvNames = @((Invoke-Az 'read UI env names' containerapp show --resource-group $ResourceGroup --name rag-ui `
            --query 'properties.template.containers[0].env[].name' --output tsv) -split "`n")
$removeEnv = if ('APP_PASSWORD' -in $uiEnvNames) { @('--remove-env-vars', 'APP_PASSWORD') } else { @() }
Invoke-Az 'update rag-ui' containerapp update --resource-group $ResourceGroup --name rag-ui --image $Image `
    --command python --args ui_start.py `
    --set-env-vars USER_TOKEN_SECRET=secretref:user-token-secret AUTH_COOKIE_SECRET=secretref:auth-cookie-secret `
    MS_CLIENT_SECRET=secretref:ms-client-secret "MS_CLIENT_ID=$(Get-EnvValue 'MS_CLIENT_ID')" "MS_TENANT=$msTenant" `
    "AUTH_REDIRECT_URI=https://$uiFqdn/oauth2callback" `
    @removeEnv --output none | Out-Null
if (Get-AppSecret 'rag-ui' 'app-password') {
    Invoke-Az 'remove obsolete UI password secret' containerapp secret remove --resource-group $ResourceGroup --name rag-ui --secret-names app-password --output none | Out-Null
}

Write-Host ''
Write-Host "Done. UI: https://$uiFqdn"
Write-Host 'Next: invite users with the invite link on the Admin page of the UI (admins: OWNER_EMAIL,'
Write-Host '  or manage_users.py admin <email>). Without an admin, use the rag-admin job, e.g.'
Write-Host "  az containerapp job start --resource-group $ResourceGroup --name rag-admin --subscription $SubscriptionId --command python --args manage_users.py invite friend@outlook.com"
Write-Host '  (add --tid/--oid for work accounts; output is in Log Analytics ContainerAppConsoleLogs_CL).'
Write-Host 'The legacy rag-ingest job / ingest workflows write unowned documents that no user can see: do not use them in v2.'
Write-Host 'Delete the rag-admin and rag-migrate jobs when not needed; they hold the admin connection string.'
# A "secret not found" lookup leaves a non-zero native exit code; every real failure already threw.
$global:LASTEXITCODE = 0
