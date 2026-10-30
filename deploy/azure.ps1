<#
.SYNOPSIS
    Provisions a small, sample-data RAG Showcase test deployment on Azure.

.DESCRIPTION
    Creates, in ONE explicitly chosen subscription: a resource group, a PostgreSQL
    Flexible Server (Burstable B1ms, pgvector), an Azure OpenAI account with one
    gpt-4.1-mini deployment, and a Container Apps environment with an internal API
    app and a public, password-protected Streamlit UI app (both scale to zero).

    Safe to re-run (resume mode): existing resources are detected and reused, missing ones
    are created, and existing credentials are never rotated. Credentials are taken from
    .env.azure when present, otherwise from the secrets of the existing rag-api app. If the
    PostgreSQL server exists but its credentials cannot be found, the script stops with
    recovery steps instead of resetting the password. On a rerun the existing apps are only
    moved to -Image; their secrets and settings are left unchanged. Transient Azure errors
    (InternalServerError, throttling, a just-deleted server name still reserved) are retried.

    Generated secrets are saved to the git-ignored .env.azure (mode 600 on
    macOS/Linux) BEFORE anything billable is created, and are never printed.
    The UI password is read from $env:RAG_UI_PASSWORD when set (CI), else prompted.

    Run with -PreflightOnly first: it performs every check and creates nothing.

    This is a prototype, not a hardened production setup (see README "Deploy to Azure").

.EXAMPLE
    pwsh ./deploy/azure.ps1 -SubscriptionId <guid> -Suffix abc123 -PreflightOnly
    pwsh ./deploy/azure.ps1 -SubscriptionId <guid> -Suffix abc123 -Image ghcr.io/adranale/rag-showcase:<commit-sha>
#>
#Requires -Version 7.0
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')][string]$SubscriptionId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{3,10}$')][string]$Suffix,
    [string]$Location = 'francecentral',
    [ValidatePattern('^[A-Za-z0-9._()-]{1,90}$')][string]$ResourceGroup = 'rg-rag-showcase',
    # Default: the image CI built for the current commit (immutable). Must be public on GHCR.
    [string]$Image = '',
    [string]$OpenAiModel = 'gpt-4.1-mini',
    [string]$OpenAiModelVersion = '2025-04-14',
    [ValidateSet('Standard', 'GlobalStandard', 'DataZoneStandard')][string]$OpenAiSku = 'GlobalStandard',
    [int]$OpenAiCapacityK = 10,
    [switch]$PreflightOnly,
    # Creates and immediately deletes a throwaway PostgreSQL server (a few cents, several minutes)
    # to prove the subscription can really provision it; Azure's SKU list gives false positives.
    [switch]$ProbePostgres,
    [switch]$AssumeYes,
    # Where to write the generated credentials (default: .env.azure in the repo root).
    [string]$EnvFile = ''
)

$ErrorActionPreference = 'Stop'
$script:SecretValues = [System.Collections.Generic.List[string]]::new()
$envFile = if ($EnvFile) { $EnvFile } else { Join-Path (Split-Path $PSScriptRoot -Parent) '.env.azure' }

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
    $transient = 'InternalServerError|ServiceUnavailable|GatewayTimeout|TooManyRequests|Too Many Requests|Specified server name is already used'
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        $output = & az @azArgs 2>&1
        if ($LASTEXITCODE -eq 0) { break }
        $text = ($output | ForEach-Object { "$_" }) -join "`n"
        if ($attempt -lt 4 -and $text -match $transient) {
            Write-Host "  transient Azure error during '$label'; retrying in $(20 * $attempt) s..."
            Start-Sleep -Seconds (20 * $attempt)
            continue
        }
        break
    }
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

function Test-AzExists {
    $label, $azArgs = $args
    try {
        Invoke-Az $label @azArgs --output none | Out-Null
        $true
    }
    catch {
        if ("$_" -match 'NotFound|not found|could not be found|does not exist') { $false } else { throw }
    }
}

function New-Secret([int]$Length = 32) {
    $chars = [char[]]'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    -join (1..$Length | ForEach-Object {
            $chars[[System.Security.Cryptography.RandomNumberGenerator]::GetInt32($chars.Length)]
        })
}

function Test-GhcrImage([string]$Ref) {
    if ($Ref -notmatch '^ghcr\.io/([a-z0-9._/-]+)(?::([\w.-]+)|@(sha256:[a-f0-9]{64}))$') { return $null }
    $repo = $Matches[1]
    $tag = if ($Matches[2]) { $Matches[2] } else { $Matches[3] }
    $token = (Invoke-RestMethod "https://ghcr.io/token?service=ghcr.io&scope=repository:${repo}:pull").token
    $headers = @{
        Authorization = "Bearer $token"
        Accept        = 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
    }
    $manifest = Invoke-RestMethod "https://ghcr.io/v2/$repo/manifests/$tag" -Headers $headers
    if ($manifest.manifests) {
        return [bool]($manifest.manifests | Where-Object {
                $_.platform.os -eq 'linux' -and $_.platform.architecture -eq 'amd64' })
    }
    $config = Invoke-RestMethod "https://ghcr.io/v2/$repo/blobs/$($manifest.config.digest)" -Headers $headers
    $config.os -eq 'linux' -and $config.architecture -eq 'amd64'
}

$problems = [System.Collections.Generic.List[string]]::new()
$manual = [System.Collections.Generic.List[string]]::new()

Write-Host '== Preflight (nothing is created yet) =='

# --- Local tools -----------------------------------------------------------
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI (az) is not installed. macOS: brew install azure-cli'
}
Invoke-Az 'install/verify az containerapp extension' extension add --name containerapp --only-show-errors | Out-Null

# --- Subscription ----------------------------------------------------------
try {
    $sub = Invoke-Az 'resolve subscription' account show --query '{name:name,id:id,tenant:tenantId,state:state}' --output json | ConvertFrom-Json
}
catch {
    throw "Subscription $SubscriptionId is not accessible with the current 'az login'. Run 'az account list --output table' to see available subscriptions."
}
if ($sub.state -ne 'Enabled') { throw "Subscription $SubscriptionId is in state '$($sub.state)', not Enabled." }

Write-Host ''
Write-Host "  Subscription:   $($sub.name)"
Write-Host "  Subscription ID: $($sub.id)"
Write-Host "  Tenant:         $($sub.tenant)"
Write-Host "  Resource group: $ResourceGroup"
Write-Host "  Region:         $Location"
Write-Host '  (Whether this subscription carries the NGO grant/credits must be checked in Azure billing.)'
Write-Host ''

# --- Region, providers -----------------------------------------------------
$locations = Invoke-Az 'list regions' account list-locations --query '[].name' --output tsv
if ($Location -notin ($locations -split "`n")) { $problems.Add("Unknown region '$Location'.") }

$providers = 'Microsoft.App', 'Microsoft.DBforPostgreSQL', 'Microsoft.CognitiveServices', 'Microsoft.OperationalInsights'
$unregistered = @()
foreach ($provider in $providers) {
    $state = Invoke-Az "read provider state $provider" provider show --namespace $provider --query registrationState --output tsv
    if ($state -ne 'Registered') { $unregistered += $provider }
}
if ($unregistered) {
    Write-Host "  Resource providers to register on deploy: $($unregistered -join ', ')"
}

# --- Existing resources / names -------------------------------------------
$pgServer = "pg-rag-$Suffix"
$aoaiName = "aoai-rag-$Suffix"
$envName = 'cae-rag-showcase'
$rgExists = (Invoke-Az 'check resource group' group exists --name $ResourceGroup) -eq 'true'
$found = [ordered]@{}
foreach ($item in @(
        @('PostgreSQL server', 'postgres', 'flexible-server', 'show', '--name', $pgServer),
        @('Azure OpenAI account', 'cognitiveservices', 'account', 'show', '--name', $aoaiName),
        @('Container Apps environment', 'containerapp', 'env', 'show', '--name', $envName),
        @('API app', 'containerapp', 'show', '--name', 'rag-api'),
        @('UI app', 'containerapp', 'show', '--name', 'rag-ui'))) {
    $itemArgs = $item[1..($item.Count - 1)]
    $found[$item[0]] = $rgExists -and (Test-AzExists "check existing $($item[0])" @itemArgs --resource-group $ResourceGroup)
}
$found['Azure OpenAI deployment'] = $found['Azure OpenAI account'] -and (Test-AzExists 'check existing Azure OpenAI deployment' `
        cognitiveservices account deployment show --resource-group $ResourceGroup --name $aoaiName --deployment-name $OpenAiModel)
$pgExists = $found['PostgreSQL server']; $aoaiExists = $found['Azure OpenAI account']
$resuming = $rgExists -and ($found.Values -contains $true)
foreach ($pair in @(@($pgServer, $pgExists), @($aoaiName, $aoaiExists))) {
    $hostName = "$($pair[0])" + $(if ($pair[0] -like 'pg-*') { '.postgres.database.azure.com' } else { '.openai.azure.com' })
    if ($pair[1]) { continue }
    try {
        [System.Net.Dns]::GetHostAddresses($hostName) | Out-Null
        $problems.Add("$hostName already resolves and is not in resource group '$ResourceGroup': the name is taken. Choose another -Suffix.")
    }
    catch [System.Net.Sockets.SocketException] { }
}
if ($resuming) {
    Write-Host "  Resume mode: existing resources in '$ResourceGroup' are reused, not recreated:"
    $found.GetEnumerator() | ForEach-Object { Write-Host ("    {0,-28} {1}" -f $_.Key, $(if ($_.Value) { 'exists' } else { 'will be created' })) }
}
elseif ($rgExists) {
    Write-Host "  Resource group '$ResourceGroup' exists but is empty of this deployment's resources; they will be created in it."
}

# --- Image -----------------------------------------------------------------
if (-not $Image) {
    $sha = (& git -C (Split-Path $PSScriptRoot -Parent) rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $sha) { throw 'Pass -Image explicitly (git commit could not be determined).' }
    $Image = "ghcr.io/adranale/rag-showcase:$sha"
}
if ($Image -match ':latest$' -or $Image -notmatch '[:@]') {
    Write-Warning "Image '$Image' is a moving tag; prefer ghcr.io/adranale/rag-showcase:<commit-sha>."
}
Write-Host "  Image:          $Image"
try {
    $amd64 = Test-GhcrImage $Image
    if ($null -eq $amd64) { $manual.Add("Image registry is not ghcr.io: verify anonymous pull and linux/amd64 manually.") }
    elseif (-not $amd64) { $problems.Add('Image has no linux/amd64 variant (required by Container Apps).') }
}
catch {
    $problems.Add("Image '$Image' cannot be pulled anonymously (missing tag, build not finished, or GHCR package not public).")
}

# --- Azure OpenAI availability and quota ----------------------------------
try {
    # Azure names the quota of gpt-4.1 models without the dash (OpenAI.GlobalStandard.gpt4.1-mini).
    $quotaModel = $OpenAiModel -replace '^gpt-4\.1', 'gpt4.1'
    $base = "https://management.azure.com/subscriptions/$SubscriptionId/providers/Microsoft.CognitiveServices/locations/$Location"
    $models = (Invoke-Az 'query model availability' rest --method get --url "$base/models?api-version=2024-10-01" --output json | ConvertFrom-Json).value
    $skus = @($models | Where-Object {
            $_.kind -eq 'OpenAI' -and $_.model.name -eq $OpenAiModel -and $_.model.version -eq $OpenAiModelVersion
        } | ForEach-Object { $_.model.skus.name })
    if ($OpenAiSku -notin $skus) {
        $problems.Add("Model $OpenAiModel $OpenAiModelVersion with $OpenAiSku is not listed for $Location. Not switching automatically; choose region/model yourself.")
    }
    $usages = (Invoke-Az 'query model quota' rest --method get --url "$base/usages?api-version=2024-10-01" --output json | ConvertFrom-Json).value
    $usage = $usages | Where-Object { $_.name.value -eq "OpenAI.$OpenAiSku.$quotaModel" } | Select-Object -First 1
    if ($found['Azure OpenAI deployment']) { }
    elseif ($null -eq $usage) { $manual.Add("OpenAI quota for $OpenAiSku $OpenAiModel could not be read; check it in the portal (AI Foundry > Quotas).") }
    elseif (($usage.limit - $usage.currentValue) -lt $OpenAiCapacityK) {
        $problems.Add("Not enough $OpenAiSku $OpenAiModel quota in $Location (free: $($usage.limit - $usage.currentValue)K, requested: ${OpenAiCapacityK}K).")
    }
}
catch {
    $manual.Add("Azure OpenAI model availability/quota could not be queried; check manually before deploying.")
}

# --- PostgreSQL availability in the region ---------------------------------
try {
    $pgCaps = (Invoke-Az 'query PostgreSQL region restrictions' postgres flexible-server list-skus --location $Location --output json | ConvertFrom-Json) | Select-Object -First 1
    if (-not $pgExists -and $pgCaps.restricted -eq 'Enabled') {
        $problems.Add("PostgreSQL Flexible Server is restricted for this subscription in $Location ($($pgCaps.reason)) Choose another -Location; not switching automatically.")
    }
}
catch {
    $manual.Add("PostgreSQL region restrictions for $Location could not be queried; verify manually.")
}

if ($ProbePostgres) {
    $probeRg = "rg-pgprobe-$(Get-Random -Minimum 100000 -Maximum 999999)"
    $probePassword = New-Secret 24
    $script:SecretValues.Add($probePassword)
    Write-Host "Probing PostgreSQL creation in $Location (temporary resource group $probeRg, deleted afterwards)..."
    try {
        Invoke-Az 'create probe resource group' group create --name $probeRg --location $Location --output none | Out-Null
        Invoke-Az 'create probe PostgreSQL server' postgres flexible-server create `
            --resource-group $probeRg --name "pgprobe$(Get-Random -Minimum 100000 -Maximum 999999)" --location $Location `
            --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 `
            --admin-user probeadmin --admin-password $probePassword `
            --public-access None --high-availability Disabled --yes --output none | Out-Null
        Write-Host '  Probe server created successfully.'
    }
    catch {
        $reason = if ("$_" -match 'SkuNotAvailable') { 'SkuNotAvailable (the subscription cannot create Standard_B1ms here)' } else { "$_" }
        $problems.Add("PostgreSQL probe failed in ${Location}: $reason")
    }
    finally {
        try { Invoke-Az 'delete probe resource group' group delete --name $probeRg --yes --output none | Out-Null }
        catch { Write-Warning "Could not delete probe resource group $probeRg. Delete it manually to avoid costs." }
    }
}

# --- Own public IP for the ingestion firewall rule ------------------------
$myIp = (Invoke-RestMethod https://api.ipify.org).ToString().Trim()
if ($myIp -notmatch '^(\d{1,3}\.){3}\d{1,3}$' -or ($myIp.Split('.') | Where-Object { [int]$_ -gt 255 })) {
    $problems.Add('Could not determine a valid public IPv4 address for the ingestion firewall rule.')
}

$manual.Add('Confirm in Azure billing that the NGO credits cover this subscription (balance, expiry, eligible services).')
Write-Host ''
if ($manual) {
    Write-Host 'Manual checks required:'
    $manual | ForEach-Object { Write-Host "  - $_" }
}
if ($problems) {
    Write-Host 'Problems:' -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    throw 'Preflight failed. Nothing was created.'
}
Write-Host 'Preflight passed.' -ForegroundColor Green
if ($PreflightOnly) { return }

# --- Confirmation and secrets ---------------------------------------------
if (-not $AssumeYes) {
    $what = if ($resuming) { 'Resume this deployment (creates only what is missing, reuses existing credentials)' } else { 'Create billable resources' }
    $answer = Read-Host "$what in subscription '$($sub.name)' ($($sub.id))? Type yes to continue"
    if ($answer -ne 'yes') { throw 'Aborted by user. Nothing was created.' }
}
# The UI password is only needed when the UI app has to be created.
$appPassword = $env:RAG_UI_PASSWORD
if (-not $appPassword -and -not $found['UI app']) {
    $appPassword = Read-Host 'Choose the UI password (8-64 chars: letters, digits and . _ ~ + @ # -)' -AsSecureString |
        ConvertFrom-SecureString -AsPlainText
}
if (($appPassword -or -not $found['UI app']) -and $appPassword -notmatch '^[A-Za-z0-9._~+@#-]{8,64}$') {
    throw 'UI password is empty or uses unsupported characters/length. Nothing was created.'
}

$pgUser = 'ragadmin'
$databaseUrl = $null
$apiKey = $null
$credentialsSaved = $false
if (Test-Path $envFile) {
    $saved = @{}
    Get-Content $envFile | ForEach-Object { $k, $v = $_ -split '=', 2; if ($v) { $saved[$k] = $v } }
    $databaseUrl = $saved['DATABASE_URL']; $apiKey = $saved['API_KEY']
    if (-not $appPassword -and $saved['APP_PASSWORD']) { $appPassword = $saved['APP_PASSWORD'] }
    $credentialsSaved = $true
    Write-Host "Reusing the credentials from $envFile (not modified)."
}
elseif ($found['API app']) {
    $secrets = Invoke-Az 'read existing app secrets' containerapp secret list --resource-group $ResourceGroup --name rag-api --show-values --output json | ConvertFrom-Json
    $databaseUrl = ($secrets | Where-Object name -eq 'db-url').value
    $apiKey = ($secrets | Where-Object name -eq 'api-key').value
    Write-Host 'Reusing the credentials stored as secrets of the existing rag-api app.'
}
elseif ($pgExists) {
    throw "PostgreSQL server '$pgServer' exists, but its credentials were not found (no $envFile and no rag-api app with a db-url secret). " +
    'The script will not reset the admin password. Recover: put DATABASE_URL and API_KEY into .env.azure, or delete the resource group ' +
    "(az group delete --name $ResourceGroup --subscription $SubscriptionId) and start again."
}
else {
    $databaseUrl = "postgresql://${pgUser}:$(New-Secret 32)@${pgServer}.postgres.database.azure.com:5432/rag?sslmode=require"
    $apiKey = New-Secret 40
}
if ($databaseUrl -notmatch "^postgresql://${pgUser}:([^@]+)@${pgServer}\.postgres\.database\.azure\.com:5432/rag\?sslmode=require$" -or -not $apiKey) {
    throw "The stored credentials do not match PostgreSQL server '$pgServer' (different -Suffix?) or are incomplete. Stopping before changing anything."
}
$pgPassword = $Matches[1]
$script:SecretValues.AddRange([string[]]@($pgPassword, $databaseUrl, $apiKey))
if ($appPassword) { $script:SecretValues.Add($appPassword) }

# Persist credentials first so a later failure cannot lose them (never overwrites an existing file).
if (-not $credentialsSaved) {
    New-Item -ItemType File -Path $envFile -ErrorAction Stop | Out-Null
    if ($IsWindows) {
        Write-Warning '.env.azure permissions are not restricted on Windows; protect the file yourself.'
    }
    else {
        & chmod 600 $envFile
        if ($LASTEXITCODE -ne 0) { throw 'Could not restrict .env.azure permissions; stopping.' }
    }
    $lines = @("DATABASE_URL=$databaseUrl", "API_KEY=$apiKey")
    if ($appPassword) { $lines += "APP_PASSWORD=$appPassword" }
    Set-Content -Path $envFile -Value $lines
    Write-Host "Credentials saved to $envFile (git-ignored)."
}

# --- Provisioning ----------------------------------------------------------
foreach ($provider in $unregistered) {
    Write-Host "Registering $provider..."
    Invoke-Az "register provider $provider" provider register --namespace $provider --wait | Out-Null
}
Invoke-Az 'create resource group' group create --name $ResourceGroup --location $Location --output none | Out-Null

if ($pgExists) { Write-Host "PostgreSQL server $pgServer exists; reusing it (credentials unchanged)." }
else {
    Write-Host 'Creating PostgreSQL (takes a few minutes)...'
    # --public-access 0.0.0.0 is Azure's "allow all Azure services" rule: ANY Azure
    # tenant's resources can reach the server (still needs the credentials and TLS).
    Invoke-Az 'create PostgreSQL server' postgres flexible-server create `
        --resource-group $ResourceGroup --name $pgServer --location $Location `
        --admin-user $pgUser --admin-password $pgPassword `
        --sku-name Standard_B1ms --tier Burstable --storage-size 32 --version 16 `
        --public-access 0.0.0.0 --yes --output none | Out-Null
}
if ($pgExists) {
    # With idle auto-stop the server may be stopped; the following steps need it running.
    $pgState = Invoke-Az 'read PostgreSQL state' postgres flexible-server show --resource-group $ResourceGroup --name $pgServer --query state --output tsv
    if ($pgState -eq 'Stopped') {
        Write-Host 'PostgreSQL server is stopped; starting it...'
        Invoke-Az 'start PostgreSQL server' postgres flexible-server start --resource-group $ResourceGroup --name $pgServer --output none | Out-Null
    }
}
Invoke-Az 'allow-list VECTOR extension' postgres flexible-server parameter set `
    --resource-group $ResourceGroup --server-name $pgServer `
    --name azure.extensions --value VECTOR --output none | Out-Null
Invoke-Az 'require TLS' postgres flexible-server parameter set `
    --resource-group $ResourceGroup --server-name $pgServer `
    --name require_secure_transport --value on --output none | Out-Null
# The database argument is --database-name in older Azure CLI versions and --name in newer ones.
try {
    Invoke-Az 'create database' postgres flexible-server db create `
        --resource-group $ResourceGroup --server-name $pgServer --database-name rag --output none | Out-Null
}
catch {
    Invoke-Az 'create database (newer CLI argument names)' postgres flexible-server db create `
        --resource-group $ResourceGroup --server-name $pgServer --name rag --output none | Out-Null
}
# Older Azure CLI versions take the server as --name and the rule as --rule-name; newer ones --server-name and --name.
try {
    Invoke-Az 'create local ingestion firewall rule' postgres flexible-server firewall-rule create `
        --resource-group $ResourceGroup --name $pgServer `
        --rule-name allow-my-ip --start-ip-address $myIp --end-ip-address $myIp --output none | Out-Null
}
catch {
    Invoke-Az 'create local ingestion firewall rule (newer CLI argument names)' postgres flexible-server firewall-rule create `
        --resource-group $ResourceGroup --server-name $pgServer `
        --name allow-my-ip --start-ip-address $myIp --end-ip-address $myIp --output none | Out-Null
}

Write-Host 'Creating Azure OpenAI...'
if (-not $aoaiExists) {
    Invoke-Az 'create Azure OpenAI account' cognitiveservices account create `
        --resource-group $ResourceGroup --name $aoaiName --location $Location `
        --kind OpenAI --sku S0 --custom-domain $aoaiName --yes --output none | Out-Null
}
if (-not $found['Azure OpenAI deployment']) {
    Invoke-Az 'create Azure OpenAI deployment' cognitiveservices account deployment create `
        --resource-group $ResourceGroup --name $aoaiName --deployment-name $OpenAiModel `
        --model-name $OpenAiModel --model-version $OpenAiModelVersion --model-format OpenAI `
        --sku-name $OpenAiSku --sku-capacity $OpenAiCapacityK --output none | Out-Null
}
$aoaiEndpoint = Assert-Value (Invoke-Az 'read Azure OpenAI endpoint' cognitiveservices account show `
        --resource-group $ResourceGroup --name $aoaiName --query properties.endpoint --output tsv) `
    'Azure OpenAI endpoint' '^https://[a-z0-9-]+\.openai\.azure\.com/?$'
$aoaiKey = Assert-Value (Invoke-Az 'read Azure OpenAI key' cognitiveservices account keys list `
        --resource-group $ResourceGroup --name $aoaiName --query key1 --output tsv) `
    'Azure OpenAI key' '^\S{20,}$'
$script:SecretValues.Add($aoaiKey)

if (-not $found['Container Apps environment']) {
    Write-Host 'Creating Container Apps environment...'
    Invoke-Az 'create Container Apps environment' containerapp env create `
        --resource-group $ResourceGroup --name $envName --location $Location --output none | Out-Null
}

if ($found['API app']) {
    Write-Host 'API app exists; moving it to the requested image (secrets and settings unchanged)...'
    Invoke-Az 'update API container app image' containerapp update `
        --resource-group $ResourceGroup --name rag-api --image $Image --output none | Out-Null
}
else {
Write-Host 'Creating API app (internal, scale to zero)...'
# The image's default command starts uvicorn on 0.0.0.0:8000.
Invoke-Az 'create API container app' containerapp create `
    --resource-group $ResourceGroup --environment $envName --name rag-api `
    --image $Image --ingress internal --target-port 8000 `
    --cpu 1 --memory 2Gi --min-replicas 0 --max-replicas 1 `
    --secrets "db-url=$databaseUrl" "aoai-key=$aoaiKey" "api-key=$apiKey" `
    --env-vars `
    DATABASE_URL=secretref:db-url `
    GENERATION_PROVIDER=azure_openai `
    "AZURE_OPENAI_ENDPOINT=$aoaiEndpoint" `
    "AZURE_OPENAI_DEPLOYMENT=$OpenAiModel" `
    AZURE_OPENAI_API_KEY=secretref:aoai-key `
    API_KEY=secretref:api-key `
    --output none | Out-Null
}
$apiFqdn = Assert-Value (Invoke-Az 'read API FQDN' containerapp show `
        --resource-group $ResourceGroup --name rag-api `
        --query properties.configuration.ingress.fqdn --output tsv) `
    'API FQDN' '^[a-z0-9.-]+\.azurecontainerapps\.io$'

if ($found['UI app']) {
    Write-Host 'UI app exists; moving it to the requested image (secrets, including the UI password, unchanged)...'
    Invoke-Az 'update UI container app image' containerapp update `
        --resource-group $ResourceGroup --name rag-ui --image $Image --output none | Out-Null
}
else {
Write-Host 'Creating UI app (public, scale to zero)...'
# az parses a space-separated value starting with "-" as an option, and a
# comma-joined value is passed as ONE argument, so the Streamlit flags are set via
# STREAMLIT_* environment variables instead. Command: streamlit run chat_ui.py
Invoke-Az 'create UI container app' containerapp create `
    --resource-group $ResourceGroup --environment $envName --name rag-ui `
    --image $Image --ingress external --target-port 8501 `
    --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 1 `
    --command streamlit --args run chat_ui.py `
    --secrets "api-key=$apiKey" "app-password=$appPassword" `
    --env-vars `
    "RAG_API_BASE_URL=https://$apiFqdn" `
    API_KEY=secretref:api-key `
    APP_PASSWORD=secretref:app-password `
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 `
    STREAMLIT_SERVER_PORT=8501 `
    STREAMLIT_SERVER_HEADLESS=true `
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false `
    --output none | Out-Null
}
$uiFqdn = Assert-Value (Invoke-Az 'read UI FQDN' containerapp show `
        --resource-group $ResourceGroup --name rag-ui `
        --query properties.configuration.ingress.fqdn --output tsv) `
    'UI FQDN' '^[a-z0-9.-]+\.azurecontainerapps\.io$'

Write-Host ''
Write-Host "UI:  https://$uiFqdn"
Write-Host 'Next: load DATABASE_URL from .env.azure and run init_db.py, ingest_text.py, embed_chunks.py (README "Deploy to Azure").'
Write-Host "When ingestion is done, remove the local firewall rule:"
Write-Host "  az postgres flexible-server firewall-rule delete --resource-group $ResourceGroup --name $pgServer --rule-name allow-my-ip --subscription $SubscriptionId --yes"
Write-Host 'Budgets only send alerts; they do not stop spending. Create one in Cost Management.'
