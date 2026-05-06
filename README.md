# oci-free-tier-monitor

Active OCI cost and resource monitor with Telegram alerts and auto-cleanup. Runs as a container, checks on a configurable schedule, and reacts to bot commands.

## Features

- **Cost alerting** — monthly spend vs a configurable GBP threshold
- **Load balancer count** — alerts when active LBs exceed the free tier limit
- **Orphaned reserved public IPs** — detects and auto-deletes unassigned IPs burning budget
- **Orphaned volumes** — detects and auto-deletes unattached boot/block volumes
- **Auto-cleanup** — enabled by default; deletes orphans automatically each check cycle
- **Telegram bot commands** — `/status`, `/scan`, `/autocleanup`, `/threshold`, `/silence`, and more
- **State persistence** — preferences saved to OCI Object Storage with local fallback
- **Cleanup reports** — each cleanup run stored as JSON in the configured bucket

## Quick start

```bash
docker run -d \
  --name oci-monitor \
  --restart unless-stopped \
  -v /path/to/appdata:/data \
  -e OCI_TENANCY_OCID=ocid1.tenancy.oc1.. \
  -e OCI_USER_OCID=ocid1.user.oc1.. \
  -e OCI_FINGERPRINT=xx:xx:xx:... \
  -e OCI_REGION=uk-london-1 \
  -e OCI_API_KEY="$(cat ~/.oci/api_key.pem)" \
  -e OCI_COMPARTMENT_OCID=ocid1.compartment.oc1.. \
  -e TELEGRAM_BOT_TOKEN=<token> \
  -e TELEGRAM_CHAT_ID=<chat_id> \
  ghcr.io/syscode-labs/oci-free-tier-monitor:latest
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OCI_TENANCY_OCID` | ✅ | — | Tenancy root OCID |
| `OCI_USER_OCID` | ✅ | — | API user OCID |
| `OCI_FINGERPRINT` | ✅ | — | API key fingerprint |
| `OCI_REGION` | ✅ | `uk-london-1` | OCI home region |
| `OCI_API_KEY` | ✅ | — | PEM private key content (full key including headers) |
| `OCI_COMPARTMENT_OCID` | ✅ | — | Compartment to monitor for resources |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | ✅ | — | Chat or user ID to send alerts to |
| `COST_THRESHOLD_GBP` | | `5.0` | Monthly spend threshold in GBP |
| `MAX_LB_COUNT` | | `1` | Max allowed active load balancers |
| `MAX_FREE_PUBLIC_IPS` | | `2` | Unassigned reserved IPs before alerting (OCI free tier: 2) |
| `CHECK_INTERVAL_HOURS` | | `6` | How often to run checks |
| `OCI_STATE_BUCKET` | | — | Object Storage bucket for state and cleanup reports |

All thresholds can also be changed at runtime via Telegram commands and are persisted to the state bucket.

## OCI IAM policy

Create a dedicated user and group, attach an API key, and apply this policy in the tenancy root:

```
Allow group oci-monitor to read usage-report in tenancy
Allow group oci-monitor to read tenancies in tenancy
Allow group oci-monitor to read objectstorage-namespaces in tenancy
Allow group oci-monitor to read all-resources in compartment <your-compartment>
Allow group oci-monitor to manage public-ips in compartment <your-compartment>
Allow group oci-monitor to manage volumes in compartment <your-compartment>
Allow group oci-monitor to manage boot-volumes in compartment <your-compartment>
Allow group oci-monitor to manage objects in compartment <your-compartment>
  where target.bucket.name = '<your-state-bucket>'
```

> The `usage-report` permission must be granted at the tenancy level as cost data is tenancy-scoped.

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`.
2. Find your chat ID: message [@userinfobot](https://t.me/userinfobot) or send any message to your bot and call `https://api.telegram.org/bot<TOKEN>/getUpdates` — the `chat.id` field is what you need.

## Bot commands

| Command | Description |
|---|---|
| `/status` | Current spend, LB count, orphaned IPs and volumes |
| `/scan` | Full resource audit — every instance, LB, IP, and orphaned volume listed |
| `/autocleanup` | Show whether auto-cleanup is on or off |
| `/autocleanup on\|off` | Enable or disable automatic deletion of orphaned resources |
| `/threshold <GBP>` | Set monthly cost alert threshold |
| `/lbmax <n>` | Set maximum allowed load balancers |
| `/silence` | Mute scheduled alerts for the current calendar month |
| `/unsilence` | Re-enable scheduled alerts |
| `/help` | Show command list |

## State and reports

When `OCI_STATE_BUCKET` is set, the monitor stores:

| Path in bucket | Contents |
|---|---|
| `oci-monitor/state.json` | Current preferences (thresholds, silence, auto-cleanup flag) |
| `oci-monitor/reports/<timestamp>.json` | Cleanup report per cycle that deleted something |

If the bucket is unreachable, the container falls back to `/data/state.json` (the bind-mounted volume). Both are always written on every preference change.

## Auto-cleanup behaviour

When enabled (default), each check cycle:

1. Orphaned reserved public IPs in `AVAILABLE` state are deleted (after allowing for `MAX_FREE_PUBLIC_IPS`)
2. Boot volumes in `AVAILABLE` state with no active attachment are deleted
3. Block (data) volumes in `AVAILABLE` state are deleted
4. A Telegram message is sent summarising what was removed
5. A JSON report is written to the state bucket

Disable with `/autocleanup off` or by setting `auto_cleanup: false` in `state.json` before starting.
