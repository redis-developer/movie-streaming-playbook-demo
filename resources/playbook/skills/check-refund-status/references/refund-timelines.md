# Refund timelines by payment channel

Timelines start from the `approvedAt` date (or `issuedAt` when present).

| Payment method | Funds visible to the customer | Notes |
|---|---|---|
| Visa / Mastercard / Amex | 5 to 10 business days after approval | Some banks show a "pending credit" within 2 days. |
| PayPal | 1 to 3 business days after issue | Goes to the PayPal balance, not the linked card. |
| Apple App Store | Apple processes it; up to 30 days | Customer must check their Apple purchase history. |
| Google Play | 3 to 5 business days | Shown under Google Pay activity. |
| Gift card / prepaid | Returned as StreamFlix credit immediately | Cannot be paid out as cash. |

If the `expectedBy` date in the refund record is later than today and the status is
still `processing`, tell the customer the refund is on track and give that date. If
`expectedBy` has passed, apologise and escalate.
