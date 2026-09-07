---
name: handoff
description: Compact the current conversation into a handoff document so another session can pick up the work. Records where things stand and what to read; never a substitute for running the gate.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

# Handoff (EDAD)

Write a document that lets a fresh session continue this work without re-deriving the
conversation. Save it to the OS temporary directory — **not** the workspace.

## This document is context, never evidence

Everything here is agent-authored prose about an agent's own work. That is exactly the
artifact class this framework refuses to trust: an implementation report is a worker
signing its own timesheet, and a handoff is the same object with a different name. It
is more dangerous than a report, not less, because it will be accurate most of the
time and so invites being believed the once it is not.

So the document states what to read and where things stood. It does **not** establish
state. The next session establishes state by running the gate:

```
python3 -m edad.gate run <TICKET> --base-ref <BASE>
```

Whatever that prints is true. Whatever this document says is a lead. When the two
disagree, the gate is right and the document is stale — say so in the document itself,
in those words, so the next reader does not have to infer it.

This is also why the file goes to the temp directory rather than the repo: the
workspace holds verified artifacts under a hash lock, and unsealed narrative does not
belong beside them. A handoff in `.edad/` would sit next to evidence and look like it.

## Reference, do not restate

Anything already captured in an artifact gets a path, not a summary. Rewriting it
creates a second version that drifts from the first, and the first is the one under
seal. Point at:

- the ticket: `.edad/tickets/<ID>.md`
- its approval lock, including the red proof: `.edad/hashes/<ID>.json`
- the grill record: `.edad/grills/<slug>.md`
- the spec: `.edad/specs/<slug>.md`
- the most recent session log: `.edad/sessions/<ID>-<stamp>.json`
- promoted evidence, if any: `.edad/evidence/<ID>.json`
- commits, branches, diffs by ref

Summarise only what exists nowhere else: what was tried and abandoned, why an approach
was rejected, what the user said they wanted that is not yet written down.

## Include

**Where the work stands.** The ticket in flight, its branch (`edad/t00N`), whether a
session ran, and its `outcome` from the session log — `passed`, `aborted`, or
`unwinnable`. These point at different next actions: `unwinnable` means the ticket was
malformed and needs re-approval, `aborted` means the agent could not do it, and
confusing them sends the next session to fix the wrong thing. A log still reading
`incomplete` means the run never reached its own ending — the process was killed — and
nothing in it should be read as a verdict.

**Worktree state.** Whether `.edad/worktrees/<ID>` still exists, and whether it holds
uncommitted work. An aborted session leaves the branch deliberately for inspection; a
next session that does not know that will delete evidence of what went wrong. Note that
a passed ticket commits its evidence *on that branch*: until it is merged, the blocker
check for downstream tickets will not see it.

**Open threads.** Decisions taken in conversation that have not reached a grill record,
spec or ticket. These are the genuinely at-risk items — everything else is on disk.
Flag them as unrecorded rather than presenting them as settled.

**Suggested skills.** Which skills the next session should invoke, and for what.
Prefer the earliest stage that still applies: if decisions are unsettled, that is
`grill-me`, not `to-tickets`.

## Redact

API keys, tokens (`CLAUDE_CODE_OAUTH_TOKEN` above all), passwords, connection strings,
and personally identifiable information. Temp directories are not private, and a
handoff is the kind of file that gets pasted into a chat window.

## Arguments

If the user passed arguments, treat them as a description of what the next session
will focus on, and tailor the document to that. Say what you left out on those
grounds, so the next reader knows the document is scoped rather than complete.
