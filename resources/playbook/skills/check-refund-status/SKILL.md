---
name: check-refund-status
description: Look up the live status of a customer's refund and explain what happens next, using the account's current refund record rather than a remembered answer.
license: Proprietary
compatibility: StreamFlix support agents with access to the lookup_refund tool
metadata:
  requires: account-id
  owner: billing-support
  reviewed: "2026-10-15"
allowed-tools: lookup_refund fetch_resource
---
# Check refund status

Use this Skill whenever a customer asks where their refund is, whether it was
approved, or when the money will arrive. The answer must come from the live
refund record. Never reuse a previous customer's answer and never guess amounts.

## Steps

1. Find the account id in the message. StreamFlix account ids look like `SF-12345`.
   If there is none, ask the customer for it and stop.
2. Call `lookup_refund(account_id)`. This returns the current refund record for the
   account (status, amount, dates, payment channel) or `hasRefund: false`.
3. Interpret the `status` field:
   - `processing` or `issued`: load `references/refund-timelines.md` to tell the
     customer when to expect the money for their payment method.
   - `denied`: load `references/refund-status-codes.md` and explain the
     `denialCode` in plain language, including what the customer can do next.
   - no refund on file: say so and point the customer to how to request one.
   You only need one of the two reference files; do not load both.
4. Reply with: the refund id, the amount and currency, the status in plain words,
   the expected date or the next step, and the payment channel the money goes to.

## Guardrails

- Quote only values present in the record. If a field is empty, say it is not available.
- Do not promise a date earlier than the timeline in the reference file.
- Do not offer to change the refund; escalate instead (see the `escalate-to-human` Skill).
