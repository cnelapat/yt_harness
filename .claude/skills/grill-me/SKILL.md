---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree and pinning every decision to a command that would fail if it were wrong. Use when the user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
---

# Grill me

Interview the user relentlessly until the two of you hold the same design — including
the parts they had not yet decided, and the parts they thought were decided but were
not. The goal is not to collect answers. It is shared understanding, recorded.

Map this as a **design tree**: every decision branches into the decisions that hang
off it. This skill is the first stage of a pipeline — its output becomes a spec, then
tickets, then frozen acceptance tests that an agent cannot edit. Anything vague here
becomes an unenforceable ticket later, so precision now is cheaper than precision
downstream.

## How to run the interview

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites
are already settled: the questions you can ask *now* without guessing at answers you
have not heard yet. A question whose answer depends on another question still open in
this round belongs to a *later* round, not this one.

**Ask the frontier, at most four questions per round.** Use `AskUserQuestion`, which
takes up to four — that cap is a feature, not a limit to work around. If the frontier
is wider, rank by cost to reverse and ask the most expensive first; the rest keep
until next round. A round of ten questions is a survey, and the user answers a survey
worse than they answer an interview.

Each round the user answers reshapes the tree: settled decisions push the frontier
outward and unblock questions that depended on them. Recompute the frontier and ask
the next round.

When writing a round in prose rather than through `AskUserQuestion`, format it:

```
❓ **Q1** — **<question title>**: <question body, may be several paragraphs, including options>
➡️ <your recommended answer>
⚖️ <trade-off — see "Calibrating the reasoning">
---
❓ **Q2** — **<question title>**: <question body>
➡️ <your recommended answer>
⚖️ <trade-off>
```

**Finding facts is your job, never the user's.** Never ask what you could read. If the
question is "does the ingest endpoint already validate site_id?", go and look. Bring
what you found *into* the question — "the ingest route already rejects a mismatched
site_id at `collectors.py:214`, so the open question is only what the collector does
with that rejection" — so the user is deciding rather than reciting. Ask only what
lives in their head: intent, priorities, constraints, risk appetite, what they want
this to become.

A running exploration is an unsettled prerequisite: only the questions downstream of
it wait. Ask the rest of the frontier now.

**Push on the soft answers.** "We'll handle that later", "it should be fine",
"probably X" — those are the places a plan breaks. Ask the follow-up. If the user
genuinely wants it deferred, get that stated as an explicit deferral with a reason,
not left as an unexamined gap.

**Re-open what an answer invalidates.** When a new answer contradicts something you
both settled earlier, say so immediately and re-open it, rather than letting the plan
quietly become inconsistent.

## Calibrating the reasoning

Always recommend. Never ask a bare question. "What should the retry policy be?" is
work handed back to the user. "I'd retry three times with jitter, because the failure
we actually see is a cold Lambda rather than a persistent outage — agree?" is a
question they can accept, refuse, or correct in one word. A question without a
recommendation is a question you have not finished thinking about.

Size the ⚖️ line to how expensive the decision is to reverse, not to how interesting
it is:

- **Cheap to reverse** (naming, formatting, ordering, anything a later edit undoes in
  minutes) — one clause on why, no alternatives enumerated.
- **Costly to reverse** (data model, public interface, dependency, storage format,
  security boundary, anything other work will be built on top of) — the case for your
  recommendation, the strongest case against it, and what would have to be true for
  the alternative to win.

If you cannot articulate what makes a decision costly to reverse, it probably is not a
design decision. Drop it from the tree rather than padding the round.

## Interrogate every answer for verifiability

This is not optional and it is not a separate pass. When the user settles a decision,
immediately ask the follow-up in the same round:

> **What command would fail if this were wrong?**

An answer is only settled once it has one. Prose criteria — "handles errors
gracefully", "is performant", "is user-friendly" — are not settled. They are
unfinished. Push until you have either:

- an executable check (a test invocation, a lint rule, a script, an exit code), or
- an explicit acknowledgement that this decision is a preference and will not be
  enforced.

Both outcomes are fine. A silent third option, where a requirement sounds testable but
nobody knows how to test it, is the one to eliminate.

Record the check **verbatim**, as a runnable command line. It becomes a frozen
acceptance command downstream — hashed, and executed by a verifier that will not run a
test that has been altered. Approximate phrasing here costs real time later, and a
command that has to be reconstructed from prose is a command nobody can freeze.

## What to grill

The mechanism above sequences the tree. This is what generates it. Work each until it
is settled or explicitly deferred:

- **The goal.** What is true after this ships that is not true now? Who notices?
- **Scope edges.** What is deliberately NOT in this? The excluded thing is usually
  where the disagreement is hiding.
- **The decision tree.** At each fork: what are the options, which do you recommend,
  what does choosing it foreclose?
- **Failure.** What happens when each part fails? Which direction is recoverable? A
  design that has not named its failure modes has only been described in the happy
  case.
- **Evidence.** How will you know it worked — and how would you know if it silently
  did not? Silent-wrong is the expensive case; name the check that catches it.
- **Interfaces and ownership.** What does this touch, who owns those, what breaks
  downstream?
- **Migration and rollback.** What happens to what exists today? Can this be undone
  after it ships?
- **Cost.** Effort, complexity carried afterwards, what it makes harder later.

## Capture scope as you go

Track which files, modules, or directories each decision touches, and ask when you
cannot infer it. Downstream this becomes a concrete allow-list that a verifier
enforces against the diff — a decision with no recorded scope produces a ticket that
cannot be scoped, and reconstructing it after the fact means re-reading the whole
conversation.

Record paths as they will be matched: real paths or globs, not descriptions.

## Log what you rejected

Whenever the user chooses against your recommendation, or picks one option over
others, record the option not taken and the stated reason in one line. This is the
cheapest it will ever be to capture, and "why didn't you do X" is the question that
has no answer six months later.

## Termination

A frontier is not empty just because you stopped thinking of questions, and it is
never empty if you keep generating branches. Stop when **every remaining open question
is cheap to reverse**. Say so explicitly:

> Remaining open: <list>. All are cheap to reverse, so I'd settle them during
> implementation rather than now. Do you agree we have shared understanding?

Then wait. Do not proceed, and do not begin producing a spec, tickets, or code, until
the user confirms in words. Their confirmation is the gate; your assessment that the
interview went well is not.

Do not stop early because the user sounds confident, and do not pad the interview to
seem thorough. Stop when the questions stop changing the plan.

## Output

On confirmation, hand off two things.

**A prose record**, in the order the decisions were made: each decision, its
verification command verbatim, the file scope it touches, the options rejected and
why, and the open questions you agreed to defer.

**A structured block**, so the next stage transcribes nothing. Downstream consumes
`scope` as path globs and `verify` as shell commands, both matched and executed
literally:

```yaml
decisions:
  - id: D1
    decision: <what was settled, one line>
    verify: <exact command line>        # omit and set `unenforced:` if it is a preference
    unenforced: <why this will not be checked>
    scope:                              # paths or globs, as they will be matched
      - <path/or/glob>
    rejected: <option not taken> — <stated reason>
deferred:
  - <open question — cheap to reverse>
```

Do not generalize the paths or commands when writing this block. Advice to keep
tickets free of file paths does not apply to these two fields: a verifier matches the
diff against `scope` and runs `verify` as written, so a path replaced by a description
or a command replaced by a summary produces a ticket that cannot be gated.

Do not editorialize the record. It is the input to the spec, and anything you smooth
over here becomes an assumption nobody notices.
