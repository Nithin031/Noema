# Starts NOEMA's OWN web-llm-hub instance, isolated from Quaero's and the standalone hub:
#   - own port (8767; the standalone hub uses 8765 which is Noema's daemon port, Quaero uses 8766)
#   - own Chrome profile + state dir, so sign-ins, locks and the "last conversation" are never shared
# First time only: sign in to DeepSeek in this profile:
#   .\scripts\noema_webllm_hub.ps1 -Login
param(
  [string]$HubRepo = "D:\Caltech Profs\WEB-LLM\web-llm-hub",
  [int]$Port = 8767,
  [string]$Provider = "deepseek",
  [switch]$Login
)
$root = Join-Path $env:LOCALAPPDATA "NOEMA-WEBLLM"
$env:WEB_LLM_PROFILE_DIR = Join-Path $root "Chrome"      # state dir = its parent
New-Item -ItemType Directory -Force -Path $env:WEB_LLM_PROFILE_DIR, (Join-Path $root "cwd") | Out-Null
Set-Location (Join-Path $root "cwd")                      # cwd-relative data/ history/ stay private to Noema
$env:PYTHONPATH = Join-Path $HubRepo "src"
if ($Login) { py -3 -m web_llm_hub.cli sessions login --providers $Provider; exit $LASTEXITCODE }
py -3 -m web_llm_hub.cli serve --host 127.0.0.1 --port $Port --init-sessions $Provider
