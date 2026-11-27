#!/usr/bin/env bash
# Creates .env and .env.gate for a real-mode run (macOS / Linux). Same output as setup_env.ps1.
# Run it yourself in a terminal: keys are typed hidden and never printed.
#
#   bash scripts/setup_env.sh
#
# Flagsmith layout (Nov 27): one project "TrustGate" (56664) with an environment per domain:
# "OpsPilot Prod" (121198) and "VaultGuard Prod" (121309). Press Enter to accept those.
# Never paste keys into chat, the board or a commit: only into this script's prompts.

set -euo pipefail
umask 077  # the env files are readable only by you

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_path="$root/.env"
gate_path="$root/.env.gate"

read_plain() {  # read_plain VAR "prompt" "default"
  local __var=$1 prompt=$2 default=${3:-} value
  if [[ -n $default ]]; then prompt="$prompt [$default]"; fi
  read -r -p "$prompt: " value || true
  value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
  printf -v "$__var" '%s' "${value:-$default}"
}

read_secret() {  # read_secret VAR "prompt" [optional]
  local __var=$1 prompt=$2 optional=${3:-} value
  while true; do
    read -r -s -p "$prompt (hidden): " value || true
    echo
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
    if [[ -n $value || -n $optional ]]; then printf -v "$__var" '%s' "$value"; return; fi
    echo "  This one is required."
  done
}

new_secret() { openssl rand -base64 32; }

env_suffix() {  # same rule as orchestrator/wiring.py server_key_name(): "OpsPilot Prod" -> OPSPILOT_PROD
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | sed -E 's/[^A-Z0-9]+/_/g; s/^_+//; s/_+$//'
}

for p in "$env_path" "$gate_path"; do
  if [[ -e $p ]]; then
    read -r -p "$(basename "$p") already exists. Overwrite it? (y/N) " answer || true
    if [[ $answer != "y" ]]; then echo "Stopped; nothing changed."; exit 1; fi
  fi
done

echo
echo "TrustGate real-mode setup. Press Enter to accept a [default]. Keys are hidden."
echo

read_plain project_name "Flagsmith project name" "TrustGate"
read_plain project_id "Flagsmith project ID" "56664"
read_plain ops_env "OpsPilot environment name" "OpsPilot Prod"
read_plain ops_env_id "OpsPilot environment ID" "121198"
read_plain vg_env "VaultGuard environment name" "VaultGuard Prod"
read_plain vg_env_id "VaultGuard environment ID" "121309"

read_plain approver_name "Your name for approvals" "naman"
read_secret approver_password "Approver password for the UI (choose one)"
signing="$(new_secret)"
gate_token="$(new_secret)"

read_secret org_key "Flagsmith ORGANISATION API key"
read_secret ops_key "OpsPilot Prod server-side key (ser.)"
read_secret vg_key "VaultGuard Prod server-side key (ser., Enter to skip)" optional

# Benchmark environments (one per runner). Optional: Enter skips one. Plain arrays: macOS
# ships bash 3.2, which has no associative arrays.
bench_names=("bench-1" "bench-2" "bench-3")
bench_ids=("121201" "121202" "121203")
bench_keys=("" "" "")
for i in 0 1 2; do
  read_secret bench_key "${bench_names[$i]} server-side key (ser., Enter to skip)" optional
  bench_keys[$i]="$bench_key"
done

# LLM: ollama (local, free; the main engine), groq (free tier, backup) or gemini.
read_plain llm_provider "LLM provider: ollama, groq or gemini" "ollama"
llm_provider="$(printf '%s' "$llm_provider" | tr '[:upper:]' '[:lower:]')"
llm_key=""
llm_base=""
case "$llm_provider" in
  ollama)
    read_plain docker "Will you run with docker compose? (Y/n)" "y"
    if [[ $docker == "n" ]]; then llm_base="http://localhost:11434"
    else llm_base="http://host.docker.internal:11434"; fi
    read_plain llm_base "Ollama URL" "$llm_base"
    echo "  Use model names exactly as 'ollama list' shows them."
    read_plain model_cheap "Ollama model for model_cheap" "qwen3:8b"
    read_plain model_flagship "Ollama model for model_flagship" "qwen3:14b"
    read_plain parallel "Parallel replays (1 unless OLLAMA_NUM_PARALLEL is raised)" "1"
    ;;
  groq)
    read_secret llm_key "Groq API key (console.groq.com > API Keys)"
    read_plain model_cheap "Groq model for model_cheap" "llama-3.1-8b-instant"
    read_plain model_flagship "Groq model for model_flagship" "llama-3.3-70b-versatile"
    read_plain parallel "Parallel replays (free tier rate limits: keep it low)" "2"
    ;;
  gemini)
    read_secret llm_key "Gemini API key (aistudio.google.com > Get API key)"
    read_plain model_cheap "Gemini model ID for model_cheap (a Flash-class model)"
    read_plain model_flagship "Gemini model ID for model_flagship (a Pro-class model)"
    read_plain parallel "Parallel replays" "4"
    ;;
  *) echo "Unknown provider '$llm_provider'; nothing written."; exit 1 ;;
esac

ops_suffix="$(env_suffix "$ops_env")"
vg_suffix="$(env_suffix "$vg_env")"

{
  echo "# Written by scripts/setup_env.sh. Never commit this file."
  echo "MOCK=0"
  echo "APPROVER_NAME=$approver_name"
  echo "APPROVER_PASSWORD=$approver_password"
  echo "APPROVAL_SIGNING_SECRET=$signing"
  echo "GATE_SHARED_TOKEN=$gate_token"
  echo "# One Flagsmith project; each agent's repairs go to its own environment."
  echo "TRUSTGATE_PROJECT=$project_name"
  echo "TRUSTGATE_ENVIRONMENT_OPSPILOT=$ops_env"
  echo "TRUSTGATE_ENVIRONMENT_VAULTGUARD=$vg_env"
  echo "FLAGSMITH_SERVER_KEY_$ops_suffix=$ops_key"
  echo "TRUSTGATE_SEARCH=sandbox"
  echo "LLM_PROVIDER=$llm_provider"
  echo "MODEL_CHEAP=$model_cheap"
  echo "MODEL_FLAGSHIP=$model_flagship"
  echo "SANDBOX_PARALLEL=$parallel"
  if [[ -n $vg_key ]]; then echo "FLAGSMITH_SERVER_KEY_$vg_suffix=$vg_key"; fi
  if [[ -n $llm_base ]]; then echo "LLM_BASE_URL=$llm_base"; fi
  if [[ -n $llm_key ]]; then echo "LLM_API_KEY=$llm_key"; fi
  for i in 0 1 2; do
    if [[ -n ${bench_keys[$i]} ]]; then
      echo "FLAGSMITH_SERVER_KEY_$(env_suffix "${bench_names[$i]}")=${bench_keys[$i]}"
    fi
  done
} > "$env_path"

environments="$ops_env:$ops_env_id"
if [[ -n $vg_key ]]; then environments="$environments,$vg_env:$vg_env_id"; fi
for i in 0 1 2; do
  if [[ -n ${bench_keys[$i]} ]]; then
    environments="$environments,${bench_names[$i]}:${bench_ids[$i]}"
  fi
done
{
  echo "# Written by scripts/setup_env.sh. Never commit this file. Only the gate reads it."
  echo "FLAGSMITH_API_KEY=$org_key"
  echo "APPROVAL_SIGNING_SECRET=$signing"
  echo "GATE_SHARED_TOKEN=$gate_token"
  echo "GATE_PROJECT_ID=$project_id"
  echo "GATE_PROJECT_NAME=$project_name"
  echo "GATE_ENVIRONMENTS=$environments"
  echo "GATE_SERVER_KEY_$ops_suffix=$ops_key"
  echo "# For scripts/release.py (plays the team shipping a release; not TrustGate's write path):"
  echo "FLAGSMITH_PROJECT_ID=$project_id"
  echo "FLAGSMITH_SERVER_KEY=$ops_key"
  if [[ -n $vg_key ]]; then echo "GATE_SERVER_KEY_$vg_suffix=$vg_key"; fi
  for i in 0 1 2; do
    if [[ -n ${bench_keys[$i]} ]]; then
      echo "GATE_SERVER_KEY_$(env_suffix "${bench_names[$i]}")=${bench_keys[$i]}"
    fi
  done
} > "$gate_path"

echo
echo "Wrote .env and .env.gate (values not shown)."
echo "The approval signing secret and the gate token were generated and put in both files."
if [[ $(git -C "$root" check-ignore .env .env.gate | wc -l) -eq 2 ]]; then
  echo "Both files are git-ignored."
else
  echo "WARNING: git does not ignore both files. Do not commit."
fi
echo "Next: docker compose up --build   (then open http://localhost:3000)"
