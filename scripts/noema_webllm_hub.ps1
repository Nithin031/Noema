# Starts a hub API for Noema that ATTACHES to the browsers you already have signed in (the same ones
# Quaero uses): default Chrome profile + default debug ports, so no new browser and no new login.
#   - own API port (8767; the standalone hub uses 8765 which is Noema's daemon port, Quaero uses 8766)
#   - own working folder, so cwd-relative data/ history/ stay out of Quaero's folders
param(
  [string]$HubRepo = "D:\Caltech Profs\WEB-LLM\web-llm-hub",
  [int]$Port = 8767,
  [string]$Provider = "deepseek"
)
$root = Join-Path $env:LOCALAPPDATA "NOEMA-WEBLLM"
New-Item -ItemType Directory -Force -Path (Join-Path $root "cwd") | Out-Null
Set-Location (Join-Path $root "cwd")
$env:PYTHONPATH = Join-Path $HubRepo "src"
py -3 -m web_llm_hub.cli serve --host 127.0.0.1 --port $Port --init-sessions $Provider
