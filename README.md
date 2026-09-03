# oci-free-tier-monitor

[![Docker](https://github.com/syscode-labs/oci-free-tier-monitor/actions/workflows/docker.yml/badge.svg)](https://github.com/syscode-labs/oci-free-tier-monitor/actions/workflows/docker.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![GHCR](https://img.shields.io/badge/container-ghcr.io-blue)
![OCI](https://img.shields.io/badge/cloud-OCI-red)

Active OCI cost and resource monitor with Telegram, webhook, and Grafana alerts plus auto-cleanup. Runs as a container, checks on a configurable schedule, and reacts to bot commands.

## Features

- **Cost alerting** — monthly spend vs a configurable GBP threshold
- **Compute free-tier limits** — scans all accessible compartments and alerts above 2 Ampere A1 instances, 2 OCPUs, 12 GB RAM, or 2 E2 Micro instances; **any non-free shape (e.g. E4.Flex) is alerted as billable**
- **Change-gated scheduled alerts** — enabled by default; repeated non-threshold findings only alert when they change
- **Load balancer count** — alerts when active LBs exceed the free tier limit
- **Orphaned reserved public IPs** — detects and auto-deletes unassigned IPs burning budget
- **Orphaned volumes** — detects and auto-deletes unattached boot/block volumes
- **Volume backup scan** — detects unexpected backups consuming storage quota
- **Custom image scan** — detects unused imported images taking up Object Storage; auto-cleanup and `/cleanup images` keep 1 unused golden image per type as a rebuild floor and delete the surplus
- **Object Storage usage** — tracks total bucket usage against the 20 GB free tier limit
- **Auto-cleanup** — enabled by default; deletes orphans automatically each check cycle
- **Month-end invoice preview** — on the last 2 days of the month, sends a one-off 🧾 message with VAT-inclusive total and per-service cost breakdown
- **Telegram bot commands** — `/status`, `/scan`, `/autocleanup`, `/threshold`, `/silence`, and more
- **State persistence** — preferences saved to OCI Object Storage with local fallback
- **Cleanup reports** — each cleanup run stored as JSON in the configured bucket

## Quick start

Three ways to run it — pick one.

### Docker run

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

### Docker Compose

Uses [`docker-compose.yml`](docker-compose.yml) at the repo root, reading credentials from your shell environment:

```bash
export OCI_TENANCY_OCID=ocid1.tenancy.oc1..
export OCI_USER_OCID=ocid1.user.oc1..
export OCI_FINGERPRINT=xx:xx:xx:...
export OCI_REGION=uk-london-1
export OCI_API_KEY="$(cat ~/.oci/api_key.pem)"
export OCI_COMPARTMENT_OCID=ocid1.compartment.oc1..
export TELEGRAM_BOT_TOKEN=<token>
export TELEGRAM_CHAT_ID=<chat_id>

docker compose up -d
```

State is stored in `./data` next to the compose file. Set `METRICS_PORT` (see [Prometheus metrics](#prometheus-metrics)) before `up` to enable the exporter — the port mapping in `docker-compose.yml` is a no-op otherwise.

### Unraid

Add the template from [`unraid/oci-monitor.xml`](unraid/oci-monitor.xml) via Community Applications' "Template URL" field, or Docker → Add Container → paste:

```text
https://raw.githubusercontent.com/syscode-labs/oci-free-tier-monitor/main/unraid/oci-monitor.xml
```

Fill in the required fields (OCI credentials, Telegram token/chat ID); everything else has a sane default. `Metrics Port` is blank/off by default — set it to enable the exporter.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OCI_TENANCY_OCID` | ✅ | — | Tenancy root OCID |
| `OCI_USER_OCID` | ✅ | — | API user OCID |
| `OCI_FINGERPRINT` | ✅ | — | API key fingerprint |
| `OCI_REGION` | ✅ | `uk-london-1` | OCI home region |
| `OCI_API_KEY` | ✅ | — | PEM private key content (full key including headers) |
| `OCI_COMPARTMENT_OCID` | ✅ | — | Compartment to monitor for compartment-scoped cleanup/storage checks; compute scans all accessible compartments and falls back to this compartment if discovery fails |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | ✅ | — | Chat or user ID to send alerts to |
| `COST_THRESHOLD_GBP` | | `5.0` | Monthly spend threshold in GBP (compared against VAT-inclusive spend) |
| `VAT_RATE` | | `0.20` | VAT rate applied to OCI ex-VAT amounts before display and threshold comparison |
| `MAX_LB_COUNT` | | `1` | Max allowed active load balancers |
| `MAX_FREE_PUBLIC_IPS` | | `2` | Unassigned reserved IPs before alerting (OCI free tier: 2) |
| `MAX_OBJECT_STORAGE_GB` | | `18.0` | Object Storage alert threshold in GB (free tier limit: 20 GB) |
| `MAX_AMPERE_INSTANCES` | | `2` | Max Always Free Ampere A1 instances |
| `MAX_AMPERE_OCPUS` | | `2` | Max total Always Free Ampere A1 OCPUs |
| `MAX_AMPERE_MEMORY_GB` | | `12` | Max total Always Free Ampere A1 memory in GB |
| `MAX_MICRO_INSTANCES` | | `2` | Max Always Free E2 Micro instances |
| `ALERT_ON_CHANGE` | | `true` | When enabled, scheduled non-threshold findings alert only when the finding set changes |
| `CHECK_INTERVAL_HOURS` | | `6` | How often to run checks |
| `OCI_STATE_BUCKET` | | — | Object Storage bucket for state and cleanup reports |
| `OCI_ACCOUNT_LABEL` | | compartment name | Display name shown in alerts and status messages (e.g. `oci@example.com-123456`) |
| `METRICS_PORT` | | — | Set to a port (e.g. `9100`) to expose a Prometheus `/metrics` endpoint. Off by default. |
| `GRAFANA_URL` | | — | Grafana base URL. Set with `GRAFANA_API_TOKEN` to publish organization-wide alert annotations. |
| `GRAFANA_API_TOKEN` | | — | Grafana service-account token with permission to create annotations. |

All thresholds can also be changed at runtime via Telegram commands and are persisted to the state bucket.

Scheduled checks always send threshold breaches and check failures. Non-threshold findings such as empty load balancers, orphaned volumes, backups, and unused custom images are sent when they first appear, change, or clear, which avoids repeating the same finding every interval.

When Grafana is configured, each scheduled alert becomes an organization-wide annotation tagged `oci-monitor` and `alert`. Verify the integration by creating a temporary threshold breach, then check Grafana's Annotations view for the matching alert. A finding is marked delivered only after at least one configured destination accepts it; failed destinations are retried on the next scheduled check.

On the last 2 days of each calendar month, a single invoice preview message is sent (regardless of threshold) showing the VAT-inclusive total and a breakdown by OCI service. It fires at most once per month.

## OCI IAM policy

Run the setup script to create the dedicated user, group, and policies automatically:

```bash
export OCI_TENANCY_OCID=ocid1.tenancy.oc1..
export OCI_COMPARTMENT_OCID=ocid1.compartment.oc1..
export OCI_STATE_BUCKET=your-state-bucket   # optional — scopes the object manage policy

mise run iam:setup
# or: make iam-setup
```

The script is idempotent — safe to re-run if policies need updating. At the end it prints a direct link to attach an API key to the created user in the Console.

> Requires the [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm) configured with credentials that have IAM write access at the tenancy root.

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

## Prometheus metrics

Set `METRICS_PORT` (e.g. `9100`) to expose `/metrics` for scraping — off by default, no extra container or image needed:

```bash
docker run -d \
  ... \
  -e METRICS_PORT=9100 \
  -p 9100:9100 \
  ghcr.io/syscode-labs/oci-free-tier-monitor:latest
```

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: oci-monitor
    static_configs:
      - targets: ["oci-monitor:9100"]
```

Metrics reflect the last completed check cycle (persisted to `/data/metrics.json`, so they survive restarts). `oci_monitor_scan_stale` is `1` if no scan has completed within 2× `CHECK_INTERVAL_HOURS` plus the daily quiet-hours window (checks pause before 09:00 Europe/London) — use it to alert on a dead or silenced monitor without nightly false positives. `oci_monitor_compute_instance_state{name,shape,state}` is the only labeled metric; everything else is a single unlabeled gauge to keep cardinality low.

## Auto-cleanup behaviour

When enabled (default), each check cycle:

1. Orphaned reserved public IPs in `AVAILABLE` state are deleted (after allowing for `MAX_FREE_PUBLIC_IPS`)
2. Boot volumes in `AVAILABLE` state with no active attachment are deleted
3. Block (data) volumes in `AVAILABLE` state are deleted
4. A Telegram message is sent summarising what was removed
5. A JSON report is written to the state bucket

Disable with `/autocleanup off` or by setting `auto_cleanup: false` in `state.json` before starting.

## Development

### Prerequisites

- [mise](https://mise.jdx.dev/) — manages `pip-tools` and `pre-commit` via pipx
- Docker (for local builds)

### Setup

```bash
mise install          # install pip-tools and pre-commit via pipx
mise run hooks:install  # install pre-commit hooks (lint + conventional commits)
```

### Tasks

| Command | Makefile equivalent | Description |
|---|---|---|
| `mise run compile` | `make compile` | Repin `requirements.txt` from `requirements.in` |
| `mise run lint` | `make lint` | Run all pre-commit checks |
| `mise run test` | `make test` | Run test suite |
| `mise run build` | `make build` | Build Docker image locally |
| `mise run iam:setup` | `make iam-setup` | Create OCI user, group, and policies |

### Adding or updating dependencies

Edit `requirements.in`, then run:

```bash
mise run compile
```

Commit both `requirements.in` and the updated `requirements.txt`.

### Dev workflow

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow diagram and commit message conventions.
