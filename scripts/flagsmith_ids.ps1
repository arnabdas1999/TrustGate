# Lists Flagsmith project and environment IDs (numbers) for setup_env.ps1.
# Asks for the organisation API key with hidden input; prints only names and IDs, never keys.
#
#   powershell -ExecutionPolicy Bypass -File scripts\flagsmith_ids.ps1

$ErrorActionPreference = "Stop"
$api = "https://api.flagsmith.com/api/v1"

$secure = Read-Host "Flagsmith ORGANISATION API key (hidden)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
$headers = @{ Authorization = "Api-Key $key" }

function Get-Items($url) {
    $response = Invoke-RestMethod -Uri $url -Headers $headers -Method Get
    if ($response.PSObject.Properties.Name -contains "results") { return $response.results }
    return $response
}

try {
    $projects = Get-Items "$api/projects/"
} catch {
    Write-Host "Flagsmith refused the key or could not be reached: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host ""
foreach ($project in $projects) {
    Write-Host ("Project  {0,-20} ID {1}" -f $project.name, $project.id) -ForegroundColor Cyan
    foreach ($environment in (Get-Items "$api/environments/?project=$($project.id)")) {
        Write-Host ("  Environment  {0,-20} ID {1}" -f $environment.name, $environment.id)
    }
}
Write-Host ""
Write-Host "Use the OpsPilot project ID and its 'Demo Production' environment ID in setup_env.ps1."
