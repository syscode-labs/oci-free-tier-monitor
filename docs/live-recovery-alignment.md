# Monitor recovery and NAT identity policy

This change brings the existing deployed recovery behavior into public source,
then applies the same NAT identity policy to scheduled alerts and status reports.
It does not deploy, change OCI resources, or enable cleanup.

## Authentication

The default `OCI_AUTH_MODE=environment` retains the existing credential variables.
Alternatively, set `OCI_AUTH_MODE=config_file`, `OCI_CONFIG_FILE` to a mounted OCI
configuration file, and optionally `OCI_PROFILE` (default `DEFAULT`). The profile
must provide tenancy, user, fingerprint, region, and a readable key_file path as
seen inside the container. Keep `OCI_COMPARTMENT_OCID` configured. Do not combine
config-file authentication with credential environment variables. Mount the
configuration and key read-only; never commit credentials.

## Reachability and incomplete scans

`OCI_PRIVATE_PROBE_HOST` optionally enables a TCP private-path probe, independently
of OCI API connectivity. `OCI_PRIVATE_PROBE_PORT` defaults to 22. This checks TCP
reachability, not application health or authentication. Leave the host unset to
disable it. A failure contributes a warning to scheduled alerts.

Identity/availability-domain/namespace preflight and compartment/object inventory
failures now fail visibly instead of silently accepting partial inventory. Any
failed resource or cost scan prevents scheduled cleanup and does not advance the
last-successful-scan timestamp. Existing deployment cleanup policy remains intact.

## NAT approval

`APPROVED_NAT_GATEWAY_IDS` is an optional comma-separated list of exact resource
identities approved by the operator. Whitespace and empty entries are ignored.
No configured identities means all discovered NAT gateways remain unapproved.
Approval does not imply Always Free eligibility or suppress other resource checks.
Names, network membership, or merely having one NAT gateway do not grant approval.
Keep actual deployment identities in private runtime configuration, not this repo.
Both scheduled alerts and status reports classify only unexpected identities as
unapproved; status reports show approved/unapproved counts.

## Publication boundary

The production monitor remains pinned to its existing local immutable image.
This PR publishes source and regression tests only. Merging can trigger the
repository's normal main-branch image build, but does not replace that pinned
production image. Deployment templates and infrastructure are unchanged.
