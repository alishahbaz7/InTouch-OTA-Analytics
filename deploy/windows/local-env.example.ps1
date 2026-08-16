# Copy to local-env.ps1 (which is gitignored) and fill in.
#
#   Copy-Item deploy\windows\local-env.example.ps1 deploy\windows\local-env.ps1
#
# These are password *verifiers* and a signing key, not the passwords themselves — but treat the
# file as a secret anyway. Do not commit it.

# ─── dashboard passwords ─────────────────────────────────────────────────────
# Generate each with (it prompts, so nothing lands in your PowerShell history):
#   .\.venv\Scripts\python.exe -m ota_analytics.cli passwd --role admin
#   .\.venv\Scripts\python.exe -m ota_analytics.cli passwd --role viewer
#
# Without the admin hash, main.py refuses to bind to anything but 127.0.0.1 — every page here
# lists IMEIs, VINs and ICCIDs, so "reachable by the whole office, protected by nothing" is a
# state the launcher will not enter.
$env:OTA_ADMIN_PASSWORD_HASH  = ""
$env:OTA_VIEWER_PASSWORD_HASH = ""

# ─── session signing key ─────────────────────────────────────────────────────
# Generate once with:
#   .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
# Optional: without it a key is generated and kept in data\secret.key instead. Changing it signs
# everyone out, which is also how you revoke every session at once.
$env:OTA_SECRET_KEY = ""

# ─── platform password (usually NOT needed on Windows) ───────────────────────
# Leave empty. The scheduler reads it from Windows Credential Manager, where the /update page
# put it. Set it here only if the scheduled task cannot reach the credential vault when nobody
# is logged in — the symptom is fetches failing with an authentication error while the same
# fetch works when you run the app by hand.
$env:OTA_PLATFORM_PASSWORD = ""

# Leave at 0. This tells the app to trust an identity header from a proxy, which is only safe
# when the app is loopback-only behind Cloudflare Access.
$env:OTA_TRUST_PROXY_AUTH = "0"
