# Creates .env and .env.gate for a real-mode run.
# Run it yourself in a terminal: keys are typed masked and never printed.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
#
# Flagsmith layout (Nov 27): one project "TrustGate" (56664) with an environment per domain:
# "OpsPilot Prod" (121198) and "VaultGuard Prod" (121309). Press Enter to accept those.
# Never paste keys into chat, the board or a commit: only into this script's prompts.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"
$gatePath = Join-Path $root ".env.gate"

function Read-Plain([string]$prompt, [string]$default = "") {
    $suffix = if ($default) { " [$default]" } else { "" }
    $value = Read-Host "$prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($value)) { return $default }
    return $value.Trim()
}

function Read-Secret([string]$prompt, [bool]$optional = $false) {
    while ($true) {
        $secure = Read-Host "$prompt (hidden)" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if ($plain -or $optional) { return $plain.Trim() }
        Write-Host "  This one is required." -ForegroundColor Yellow
    }
}

function New-Secret {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes)
}

function Get-EnvSuffix([string]$name) {
    # Same rule as orchestrator/wiring.py server_key_name(): "OpsPilot Prod" -> OPSPILOT_PROD
    return ($name.ToUpper() -replace "[^A-Z0-9]+", "_").Trim("_")
}

function Write-EnvFile([string]$path, [string[]]$lines) {
    # UTF-8 without a BOM: Docker's env_file and uv's --env-file both read it cleanly.
    [IO.File]::WriteAllLines($path, $lines, (New-Object Text.UTF8Encoding $false))
}

foreach ($p in @($envPath, $gatePath)) {
    if (Test-Path $p) {
        $answer = Read-Host "$(Split-Path -Leaf $p) already exists. Overwrite it? (y/N)"
        if ($answer -ne "y") { Write-Host "Stopped; nothing changed."; exit 1 }
    }
}

Write-Host ""
Write-Host "TrustGate real-mode setup. Press Enter to accept a [default]. Keys are hidden." -ForegroundColor Cyan
Write-Host ""

$projectName = Read-Plain "Flagsmith project name" "TrustGate"
$projectId = Read-Plain "Flagsmith project ID" "56664"
$opsEnv = Read-Plain "OpsPilot environment name" "OpsPilot Prod"
$opsEnvId = Read-Plain "OpsPilot environment ID" "121198"
$vgEnv = Read-Plain "VaultGuard environment name" "VaultGuard Prod"
$vgEnvId = Read-Plain "VaultGuard environment ID" "121309"

$approverName = Read-Plain "Your name for approvals" "arnab"
$approverPassword = Read-Secret "Approver password for the UI (choose one)"
$signing = New-Secret

$orgKey = Read-Secret "Flagsmith ORGANISATION API key (arnab-gate)"
$opsKey = Read-Secret "OpsPilot Prod server-side key (ser.)"
$vgKey = Read-Secret "VaultGuard Prod server-side key (ser., Enter to skip)" $true

# LLM: ollama (local, free; the main engine), groq (free tier, backup) or gemini.
$llmProvider = (Read-Plain "LLM provider: ollama, groq or gemini" "ollama").ToLower()
$llmKey = ""
$llmBase = ""
switch ($llmProvider) {
    "ollama" {
        $docker = Read-Plain "Will you run with docker compose? (Y/n)" "y"
        $llmBase = if ($docker -eq "n") { "http://localhost:11434" } else { "http://host.docker.internal:11434" }
        $llmBase = Read-Plain "Ollama URL" $llmBase
        Write-Host "  Use model names exactly as 'ollama list' shows them."
        $modelCheap = Read-Plain "Ollama model for model_cheap" "qwen3:8b"
        $modelFlagship = Read-Plain "Ollama model for model_flagship" "qwen3:14b"
        $parallel = Read-Plain "Parallel replays (1 unless OLLAMA_NUM_PARALLEL is raised)" "1"
    }
    "groq" {
        $llmKey = Read-Secret "Groq API key (console.groq.com > API Keys)"
        $modelCheap = Read-Plain "Groq model for model_cheap" "llama-3.1-8b-instant"
        $modelFlagship = Read-Plain "Groq model for model_flagship" "llama-3.3-70b-versatile"
        $parallel = Read-Plain "Parallel replays (free tier rate limits: keep it low)" "2"
    }
    "gemini" {
        $llmKey = Read-Secret "Gemini API key (aistudio.google.com > Get API key)"
        $modelCheap = Read-Plain "Gemini model ID for model_cheap (a Flash-class model)"
        $modelFlagship = Read-Plain "Gemini model ID for model_flagship (a Pro-class model)"
        $parallel = Read-Plain "Parallel replays" "4"
    }
    default { Write-Host "Unknown provider '$llmProvider'; nothing written."; exit 1 }
}
$gateToken = New-Secret

$opsSuffix = Get-EnvSuffix $opsEnv
$vgSuffix = Get-EnvSuffix $vgEnv

$envLines = @(
    "# Written by scripts/setup_env.ps1. Never commit this file.",
    "MOCK=0",
    "APPROVER_NAME=$approverName",
    "APPROVER_PASSWORD=$approverPassword",
    "APPROVAL_SIGNING_SECRET=$signing",
    "GATE_SHARED_TOKEN=$gateToken",
    "# One Flagsmith project; each agent's repairs go to its own environment.",
    "TRUSTGATE_PROJECT=$projectName",
    "TRUSTGATE_ENVIRONMENT_OPSPILOT=$opsEnv",
    "TRUSTGATE_ENVIRONMENT_VAULTGUARD=$vgEnv",
    "FLAGSMITH_SERVER_KEY_$opsSuffix=$opsKey",
    "TRUSTGATE_SEARCH=sandbox",
    "LLM_PROVIDER=$llmProvider",
    "MODEL_CHEAP=$modelCheap",
    "MODEL_FLAGSHIP=$modelFlagship",
    "SANDBOX_PARALLEL=$parallel"
)
if ($llmBase) { $envLines += "LLM_BASE_URL=$llmBase" }
if ($llmKey) { $envLines += "LLM_API_KEY=$llmKey" }
if ($vgKey) { $envLines += "FLAGSMITH_SERVER_KEY_$vgSuffix=$vgKey" }

$environments = "${opsEnv}:$opsEnvId"
if ($vgKey) { $environments += ",${vgEnv}:$vgEnvId" }
$gateLines = @(
    "# Written by scripts/setup_env.ps1. Never commit this file. Only the gate reads it.",
    "FLAGSMITH_API_KEY=$orgKey",
    "APPROVAL_SIGNING_SECRET=$signing",
    "GATE_SHARED_TOKEN=$gateToken",
    "GATE_PROJECT_ID=$projectId",
    "GATE_PROJECT_NAME=$projectName",
    "GATE_ENVIRONMENTS=$environments",
    "GATE_SERVER_KEY_$opsSuffix=$opsKey",
    "# For scripts/release.py (plays the team shipping a release; not TrustGate's write path):",
    "FLAGSMITH_PROJECT_ID=$projectId",
    "FLAGSMITH_SERVER_KEY=$opsKey"
)
if ($vgKey) { $gateLines += "GATE_SERVER_KEY_$vgSuffix=$vgKey" }

Write-EnvFile $envPath $envLines
Write-EnvFile $gatePath $gateLines

Write-Host ""
Write-Host "Wrote .env and .env.gate (values not shown)." -ForegroundColor Green
Write-Host "The approval signing secret and the gate token were generated and put in both files."
$ignored = git -C $root check-ignore .env .env.gate
if (($ignored | Measure-Object).Count -eq 2) {
    Write-Host "Both files are git-ignored." -ForegroundColor Green
} else {
    Write-Host "WARNING: git does not ignore both files. Do not commit." -ForegroundColor Red
}
Write-Host "Next: docker compose up --build   (then open http://localhost:3000)"
