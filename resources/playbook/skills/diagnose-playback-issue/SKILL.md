---
name: diagnose-playback-issue
description: Diagnose buffering, blurry video or playback error codes using the account's live stream diagnostics, then give device-specific fixes.
license: Proprietary
metadata:
  requires: account-id
  owner: playback-support
  reviewed: "2026-10-20"
allowed-tools: get_stream_diagnostics fetch_resource
---
# Diagnose a playback issue

Use this Skill when a customer reports buffering, low quality, freezing or an error
code. The diagnosis must be based on the account's current stream diagnostics, not
on generic advice.

## Steps

1. Find the account id (`SF-12345`). If missing, ask for it and stop.
2. Call `get_stream_diagnostics(account_id)`. It returns the last device, app
   version, measured bandwidth, current quality and the last error code.
3. If `lastErrorCode` is set, load `references/error-codes.md` and explain that
   code with its fix. Otherwise load `references/bitrate-requirements.md` and compare
   `measuredBandwidthMbps` with what the plan's quality needs.
4. Reply with: what you observed (device, bandwidth, quality), the likely cause, and
   at most three concrete steps for that device.

## Guardrails

- Do not blame the customer's internet unless the measured bandwidth is below the
  requirement for their plan.
- If the app version is older than 9.4, recommend updating before anything else.
