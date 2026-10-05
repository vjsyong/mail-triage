# Sent-reply resolution

The needs-reply queue represents outstanding responses, not just the classifier's
initial verdict. A message progresses through awaiting reply -> matched sent reply
-> answered / partial / uncertain. New incoming messages have independent states.

## Matching and assessment

- Poll IMAP Sent special-use folders; fall back to conventional Sent Items/Sent
  names. A configured `reply_sent_folder` may override discovery. Draft folders
  never qualify. Scanning is incremental, with UIDVALIDITY-aware checkpoints and
  an initial 30-day window; header evidence remains available for late classification.
- The search indexer also captures thread headers, so moved or late-classified
  messages keep their links. A cached draft whose Message-ID is later sent is
  promoted in place: the sent copy becomes canonical, and a stale location is
  rescued by Message-ID lookup before assessment.
- Match normalized Message-ID against In-Reply-To/References, corroborating the
  account owner's sender address, recipient, and chronological ordering. A shared
  subject alone does not clear anything. Auto-Submitted messages do not count.
- Assess only newly authored text, excluding quoted history. Meaningful answers
  or actionable next steps may satisfy a request; a holding acknowledgement does
  not. Incomplete bodies, malformed output, failures or low confidence keep the flag.
- Clearing requires a strict sufficient verdict, no unanswered requests, and
  confidence >= 0.90. The original and sent evidence are audited with a reason.
- Use the configured classifier endpoint/fallback and existing hourly budget, up
  to three checks per worker pass. LLM classification off pauses assessments.

## Persistence and manual intent

No schema migration: thread headers and reply-state transitions use `msg_events`;
scanner checkpoints use existing settings. Pair assessments are idempotent. Errors
may retry after a cooldown; new replies are new candidates. Updating a manual
correction during an assessment prevents that result from overwriting it.

An answered state survives reclassification. Manually marking Needs reply after a
resolution reopens it and requires a newer sent response. Manual No reply needed
remains authoritative. Resolving a response is an outcome, not evidence that the
original never needed a reply; training retains the original positive requirement.

## UI

The viewer shows the latest reply state, reason, confidence and linked sent
message. Sent messages link back to the incoming request(s) they addressed. Check
sent replies queues a worker pass. The existing Needs reply button reopens a
mistaken resolution. AI settings expose the automatic detection switch.

## Scope

This reconciliation changes local triage flags and audit evidence. Existing mail
actions and account configuration are not part of this feature. Replies from the
primary IMAP identity are recognized; optional `reply_identity_addresses` settings
can declare sending aliases without altering the account.
