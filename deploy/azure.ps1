<#
.SYNOPSIS
    Provisions the RAG Showcase on Azure at minimum cost.

.DESCRIPTION
    Creates: resource group, PostgreSQL Flexible Server (Burstable B1ms, pgvector),
    Azure OpenAI (capped gpt-4o-mini deployment), and a Container Apps environment
    with an internal API app and a public UI app (both scale to zero).

    Prerequisites: Azure CLI (`az login` done) and a PUBLIC container image
    (see .github/workflows/build-image.yml; set the GHCR package to public once).

    Secrets are generated here, stored as Container Apps secrets, and written to
    the git-ignored file .env.azure. They are never printed.

.EXAMPLE
    .\deploy\azure.ps1 -Suffix abc123
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{3,10}$')][string]$Suffix,
    [string]$Location = 'westeurope',
    [string]$ResourceGroup = 'rg-rag-showcase',
    [string]$Image = 'ghcr.io/adranale/rag-showcase:latest',
    [string]$OpenAiModel = 'gpt-4o-mini',
    [string]$OpenAiModelVersion = '2024-07-18',
    [int]$OpenAiCapacityK = 10
)

$ErrorActionPreference = 'Stop'

function Invoke-Az {
    & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
}

function New-Secret([int]$Length = 32) {
    $chars = [char[]]'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    -join (1..$Length | ForEach-Object {
            $chars[[System.Security.Cryptography.RandomNumberGenerator]::GetInt32($chars.Length)]
        })
}

$pgServer = "pg-rag-$Suffix"
$pgUser = 'ragadmin'
$pgPassword = New-Secret 32
$aoaiName = "aoai-rag-$Suffix"
$envName = 'cae-rag-showcase'
$apiKey = New-Secret 40
$appPassword = Read-Host 'Choose the UI password' -AsSecureString
$appPasswordPlain = [System.Net.NetworkCredential]::new('', $appPassword).Password

Invoke-Az extension add --name containerapp --upgrade --only-show-errors
Invoke-Az group create --name $ResourceGroup --location $Location --output none

Write-Host 'Creating PostgreSQL (takes a few minutes)...'
# --public-access 0.0.0.0 allows Azure-hosted services (the Container Apps).
Invoke-Az postgres flexible-server create `
    --resource-group $ResourceGroup --name $pgServer --location $Location `
    --admin-user $pgUser --admin-password $pgPassword `
    --sku-name Standard_B1ms --tier Burstable --storage-size 32 --version 16 `
    --public-access 0.0.0.0 --yes --output none
Invoke-Az postgres flexible-server parameter set `
    --resource-group $ResourceGroup --server-name $pgServer `
    --name azure.extensions --value VECTOR --output none
Invoke-Az postgres flexible-server db create `
    --resource-group $ResourceGroup --server-name $pgServer `
    --database-name rag --output none

# Lets this machine run the ingestion scripts against the database.
$myIp = (Invoke-RestMethod https://api.ipify.org).Trim()
Invoke-Az postgres flexible-server firewall-rule create `
    --resource-group $ResourceGroup --name $pgServer `
    --rule-name allow-my-ip --start-ip-address $myIp --end-ip-address $myIp --output none

Write-Host 'Creating Azure OpenAI...'
# If the model/version/SKU is unavailable in your region, adjust the parameters.
Invoke-Az cognitiveservices account create `
    --resource-group $ResourceGroup --name $aoaiName --location $Location `
    --kind OpenAI --sku S0 --custom-domain $aoaiName --yes --output none
Invoke-Az cognitiveservices account deployment create `
    --resource-group $ResourceGroup --name $aoaiName --deployment-name $OpenAiModel `
    --model-name $OpenAiModel --model-version $OpenAiModelVersion --model-format OpenAI `
    --sku-name GlobalStandard --sku-capacity $OpenAiCapacityK --output none
$aoaiEndpoint = (az cognitiveservices account show `
        --resource-group $ResourceGroup --name $aoaiName `
        --query properties.endpoint --output tsv).Trim()
$aoaiKey = (az cognitiveservices account keys list `
        --resource-group $ResourceGroup --name $aoaiName `
        --query key1 --output tsv).Trim()

$databaseUrl = "postgresql://${pgUser}:${pgPassword}@${pgServer}.postgres.database.azure.com:5432/rag?sslmode=require"

Write-Host 'Creating Container Apps environment...'
Invoke-Az containerapp env create `
    --resource-group $ResourceGroup --name $envName --location $Location --output none

Write-Host 'Creating API app (internal, scale to zero)...'
Invoke-Az containerapp create `
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
    --output none

$apiFqdn = (az containerapp show --resource-group $ResourceGroup --name rag-api `
        --query properties.configuration.ingress.fqdn --output tsv).Trim()

Write-Host 'Creating UI app (public, scale to zero)...'
Invoke-Az containerapp create `
    --resource-group $ResourceGroup --environment $envName --name rag-ui `
    --image $Image --ingress external --target-port 8501 `
    --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 1 `
    --command python `
    --args=-m,streamlit,run,chat_ui.py,--server.address=0.0.0.0,--server.port=8501,--server.headless=true `
    --secrets "api-key=$apiKey" "app-password=$appPasswordPlain" `
    --env-vars `
    "RAG_API_BASE_URL=https://$apiFqdn" `
    API_KEY=secretref:api-key `
    APP_PASSWORD=secretref:app-password `
    --output none

$uiFqdn = (az containerapp show --resource-group $ResourceGroup --name rag-ui `
        --query properties.configuration.ingress.fqdn --output tsv).Trim()

# Used for local ingestion: $env:DATABASE_URL = ... (see README).
Set-Content -Path (Join-Path $PSScriptRoot '..\.env.azure') -Value "DATABASE_URL=$databaseUrl"

Write-Host ''
Write-Host "UI:  https://$uiFqdn"
Write-Host 'Next: run init_db.py and the ingestion scripts with DATABASE_URL from .env.azure (see README).'
Write-Host 'Also create a cost budget in the Azure portal (Cost Management > Budgets).'
