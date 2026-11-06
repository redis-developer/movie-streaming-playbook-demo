---
name: escalate-to-human
description: Decide when a support request must go to a human agent and hand it over with the right context.
license: Proprietary
metadata:
  owner: support-operations
allowed-tools: fetch_resource
---
# Escalate to a human agent

Load `references/escalation-matrix.md` to check whether the request matches an
escalation trigger. If it does, tell the customer you are handing over, summarise what
you already know (account id, issue, what was tried) and give the expected response
time from the matrix. Do not attempt refunds, account changes or fraud decisions
yourself.
