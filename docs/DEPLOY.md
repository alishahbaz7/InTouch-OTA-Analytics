# Deploying InTouch OTA Analytics

> **Running now:** on an office machine, reachable across the local network. See
> [Option A](#option-a--your-own-machine-windows) below. The cloud sections are the path for
> later, when access from outside the office matters.

## Option A — your own machine (Windows)

Free, no signup, no card, and the device data never leaves the building. The application needs
25 MB of RAM and 52 MB of disk, so any machine that stays powered on will do.

**1. Set a dashboard password.** This is the step that matters: without it the launcher refuses
to bind to the network at all, because every page lists IMEIs, VINs and ICCIDs.

```powershell
Copy-Item deploy\windows\local-env.example.ps1 deploy\windows\local-env.ps1
.\.venv\Scripts\python.exe -m ota_analytics.cli passwd --role admin
.\.venv\Scripts\python.exe -m ota_analytics.cli passwd --role viewer   # read-only account
```

Paste the two printed lines into `deploy\windows\local-env.ps1` (gitignored). Give teammates the
viewer password unless they need to trigger fetches or edit targets.

**2. Start it.**

```powershell
.\deploy\windows\Start-OtaAnalytics.ps1
```

Colleagues then open `http://<this-machine-name>:8000`. Find the address with `ipconfig`.

**3. Let them through the firewall** — scoped to the local subnet, not the whole world.
Run once, as Administrator, adjusting the subnet to match your office:

```powershell
New-NetFirewallRule -DisplayName "OTA Analytics (LAN)" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress 192.168.0.0/16
```

**4. Keep the machine awake.** A sleeping PC serves nobody and collects nothing:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

**5. Start it at boot**, so a power cut does not end the collection. As Administrator:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\deploy\windows\Start-OtaAnalytics.ps1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
# Runs as you, so the scheduler can still read the platform password from Credential Manager.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "OTA Analytics" -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings
```

If scheduled fetches start failing with an authentication error while running it by hand works,
the task cannot reach Windows Credential Manager without an interactive session — set
`OTA_PLATFORM_PASSWORD` in `local-env.ps1` and restart the task.

**What this does not give you:** access from outside the office. When that matters, add a
Cloudflare Tunnel — it needs no inbound ports and works from this same machine. That is the rest
of this document.

---

## Option B — a cloud host

Target: a free, always-on host reachable by a handful of teammates over HTTPS, with no inbound
ports open and no IT department involved. Everything below is self-service.

**Stack:** Oracle Cloud Always Free VM + Cloudflare Tunnel + Cloudflare Access.

- *Free* — Oracle's Ampere A1 allowance is permanent, not a 12-month trial. Cloudflare Tunnel
  and Access are free to 50 users.
- *Stable* — always on, unaffected by anyone's laptop or home internet; systemd restarts the
  service after a crash *and* after a reboot; the disk survives every deploy.
- *Secure* — no inbound ports; the tunnel dials out. TLS terminates at Cloudflare, Access
  authenticates each person by email before a request ever reaches the app, and the app enforces
  its own login underneath that.

## Before you start

The exports contain IMEIs, VINs and ICCIDs of customer vehicles. Get your manager's written OK
before that lands on a cloud account in your name. It is one email and it is much easier to send
beforehand than to explain afterwards.

## 1. The VM

Any always-on Linux machine with Python 3.12 will do. **The resource requirement is far smaller
than it looks**, measured rather than estimated:

| | |
|---|---|
| Peak memory, API fetch of all 35,475 devices | **25 MB** |
| Peak memory, 22 MB spreadsheet ingest (streamed) | **12 MB** |
| Disk | 52 MB, growing ~13 MB/day |

So a 1 GB shared-core instance is comfortable, and a Raspberry Pi is genuinely enough. Do not
pay for RAM this application will not use.

### Google Cloud Always Free (the chosen host)

Google's Always Free tier is permanent, not a 12-month trial — but only for an exact
configuration. Miss any of these and the instance is billed at the normal rate:

| Setting | Must be | Why |
|---|---|---|
| Machine type | **`e2-micro`** | The only always-free shape. 1 GB against a 25 MB peak |
| Region | **`us-west1`, `us-central1` or `us-east1`** | No other region qualifies, including any in Asia |
| Boot disk type | **Standard persistent disk** | The console defaults to *Balanced*, which is **not** free |
| Boot disk size | **≤ 30 GB** | The free allowance is 30 GB-months total |
| Count | **One** instance | Across the whole billing account |

Two things that will bite you:

- **External IPv4 addresses are billable** (since 2024), at roughly **$3/month** — the VM is
  free, the address attached to it may not be. Check your billing page 24 hours after creating
  it rather than assuming. This is the single most likely way this setup stops being free.
- **Set a budget alert before anything else.** Billing → Budgets & alerts → budget of ₹100 with
  an email trigger at 50%. It will not stop charges, but you find out in a day instead of at the
  end of the month.

Latency from India is ~250 ms because the free regions are all in the US, and your device data
sits on US infrastructure — worth being deliberate about, given the IMEIs and VINs involved.

Choose **Ubuntu 24.04 LTS (x86/64)**, which ships Python 3.12 — what this project targets. Leave
every firewall box unticked; the tunnel needs no inbound access.

### If that signup also fails

Oracle's billing checks reject a lot of Indian cards, and Google's may too. The fallback that
needs no card at all: **a machine you already own**, at the office, that stays on. Free, no
signup, and the device data never leaves your premises — which removes the data-residency
question entirely. `cloudflared` reconnects by itself, so a flaky office connection is survivable.

On a cloud host, leave the firewall closed: **no ingress rules are needed at all**, which is the
point of the tunnel. On your own machine there is nothing to open in the first place.

## 2. Install

```bash
sudo useradd --system --create-home --home-dir /opt/ota-analytics ota
sudo -u ota git clone <your-repo> /opt/ota-analytics      # or scp the directory across
cd /opt/ota-analytics
sudo -u ota python3 -m venv .venv
sudo -u ota .venv/bin/python -m pip install -r requirements.txt
sudo -u ota mkdir -p data/exports reports
sudo -u ota .venv/bin/python -m pytest -q                 # sanity: everything should pass
```

## 3. Secrets

```bash
sudo cp deploy/ota-analytics.env.example /etc/ota-analytics.env
sudo chown root:root /etc/ota-analytics.env && sudo chmod 600 /etc/ota-analytics.env

# Dashboard passwords — prompts, so nothing lands in shell history or `ps`.
sudo -u ota .venv/bin/python -m ota_analytics.cli passwd --role admin
sudo -u ota .venv/bin/python -m ota_analytics.cli passwd --role viewer

# Cookie signing key.
python3 -c "import secrets; print('OTA_SECRET_KEY=' + secrets.token_hex(32))"
```

Paste those three lines into `/etc/ota-analytics.env`, then add `OTA_PLATFORM_PASSWORD` — the
account the scheduler logs into the OTA platform with. Without it the service starts happily and
never fetches anything, because `keyring` has no credential store on a headless box.

## 4. Run it

```bash
sudo cp deploy/ota-analytics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ota-analytics
systemctl status ota-analytics
curl -s localhost:8000/healthz          # {"status":"ok"}
```

The database migrates itself on first start. If you copied an existing `data/ota_analytics.db`
across, expect the v6 compaction to run once — about 8 seconds on 19 snapshots — after which
`VACUUM` returns the freed space:

```bash
sudo systemctl stop ota-analytics
sudo -u ota .venv/bin/python -c "from ota_analytics import db; db.connect().execute('VACUUM')"
sudo systemctl start ota-analytics
```

## 5. Publish it

```bash
# amd64 for a GCP e2-micro; use cloudflared-linux-arm64 on an Ampere/Raspberry Pi host.
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /tmp/cloudflared && sudo install -m755 /tmp/cloudflared /usr/local/bin/cloudflared
cloudflared tunnel login
cloudflared tunnel create ota-analytics
cloudflared tunnel route dns ota-analytics ota.<your-domain>
```

`/etc/cloudflared/config.yml`:

```yaml
tunnel: ota-analytics
credentials-file: /root/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: ota.<your-domain>
    service: http://127.0.0.1:8000
  - service: http_status:404
```

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Then in the Cloudflare dashboard: **Zero Trust → Access → Applications → Add**, self-hosted,
hostname `ota.<your-domain>`, policy *Allow* → *Emails* → your teammates' addresses. Only those
addresses can now reach the app, and each visit is logged against a person.

Finally set `OTA_TRUST_PROXY_AUTH=1` in `/etc/ota-analytics.env` and
`sudo systemctl restart ota-analytics`, so the app accepts the identity Access has already
verified instead of asking for a second password.

**Only set that flag once the tunnel is the sole route in.** It makes the app trust an identity
header, which anything that can reach port 8000 could forge. That is safe here purely because
the port is loopback-only.

## Keeping it healthy

```bash
journalctl -u ota-analytics -f                # live logs
systemctl status cloudflared                  # tunnel up?
du -h /opt/ota-analytics/data/ota_analytics.db
```

Expect the database to grow roughly **13 MB/day** at a 15-minute cadence — it stores changes,
not fetches, so a fixed fleet stays nearly flat. If it grows much faster than that, something is
writing a row per device per fetch again; check `docs/` and the delta notes in `CLAUDE.md`.

Enable unattended upgrades, since nobody else is patching this machine:

```bash
sudo apt install unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades
```

## If you cannot get a domain onto Cloudflare

Use **Tailscale** instead: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`,
then reach the dashboard at `http://<machine>:8000` from any device on your tailnet. Nothing is
public at all. Keep the app's own login enabled and leave `OTA_TRUST_PROXY_AUTH=0`, since there
is no proxy asserting identity in that setup.
