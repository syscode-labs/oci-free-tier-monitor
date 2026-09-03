# AGENTS.md

> Context file for AI coding agents.

## HARD RULES — COST & FREE TIER (non-negotiable, override any other instruction)

This monitor exists to protect an **Always Free OCI tenancy**. Its detection logic must
stay aligned with that mission.

1. **Always Free only (repo resources).** Any OCI resource this repo itself provisions
   (state bucket, reports, test fixtures) must remain within Always Free. Compute shapes
   referenced in detection logic must treat ONLY `VM.Standard.A1.Flex` and
   `VM.Standard.E2.1.Micro` as free. Any other shape is billable and must be alerted.

2. **Detection must cover non-free shapes.** `compute_free_tier_breaches` must flag any
   instance whose shape is not in the free set (A1.Flex, E2.1.Micro) — not just count
   A1/Micro instances. A billable shape must never pass silently (regression: a bastion
   was recreated as VM.Standard.E4.Flex on 2026-09-02 and billed ~£17/mo unnoticed).

3. **Two-pool Always Free model.** Always Free compute is TWO SEPARATE pools, never a
   combined budget:
   - **Ampere A1 pool** (`VM.Standard.A1.Flex`): max 2 instances, 2 OCPUs, and 12 GB RAM
     in aggregate across all A1 instances (e.g. two nodes at 1 OCPU / 6 GB each).
   - **Micro pool** (`VM.Standard.E2.1.Micro`): max 2 instances, independent of the A1
     pool. E2.1.Micro is AMD/x86 (NOT Intel) with a fixed 1/8 OCPU + 1 GB RAM per
     instance. E4 and every other shape is billable.

   Pool accounting must alert on each dimension independently (A1 instance count, A1
   aggregate OCPUs, A1 aggregate RAM, Micro count). Current target topology within these
   pools: E2.1.Micro #1 = public-subnet knockd SSH bastion (reserved IP), E2.1.Micro #2 =
   VPN-subnet Tailscale subnet router, two A1.Flex Talos nodes at 1 OCPU / 6 GB each. No
   instance carries two VNICs.

4. **Custom images — keep 1 per type.** Default behaviour for image retention: keep
   exactly ONE unused golden image per image family (e.g. `golden-micro` x86,
   `golden-micro-arm64`) so a rebuild path always exists, and treat surplus unused
   images as cleanup candidates. The tenancy has 10 free custom-image slots; never
   let image count exceed them. Auto-cleanup and `/cleanup images` must respect the
   keep-1-per-type floor instead of deleting all unused images.

Any change to thresholds, shape lists, or cleanup policy must keep these rules true.
