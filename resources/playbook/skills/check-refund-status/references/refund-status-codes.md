# Refund status and denial codes

## Status values

- `requested`: the customer asked, nobody has reviewed it yet (usually under 24 hours).
- `processing`: approved and sent to the payment provider.
- `issued`: the provider confirmed the credit.
- `denied`: reviewed and refused; see the denial code.

## Denial codes

| Code | Meaning | What the customer can do |
|---|---|---|
| OUTSIDE-WINDOW | The request came more than 14 days after the charge. | Nothing further on this charge; they can cancel to avoid the next one. |
| PARTIAL-USE | More than 50% of the billing period was used before the request. | A prorated credit may be offered once per year; escalate to a human. |
| PROMO-NONREFUNDABLE | The charge was a discounted promotional period. | Not refundable; explain the promo terms. |
| CHARGEBACK-OPEN | The customer's bank already opened a dispute. | The bank resolves it; StreamFlix cannot refund in parallel. |
| FRAUD-REVIEW | The account is under a fraud review. | Escalate to a human immediately. |
