# EDAD — quickstart

A gate runner for agent-written code. A ticket names the acceptance tests an
agent must make pass, the files it may touch, and the conditions under which
it is stopped. The tests are hashed at approval and the agent cannot edit
them; a run is judged by the gate, never by the agent's own report.

The harness is a tool. It runs *against* a target repo — the one whose code
the agent changes — and keeps that repo's tickets, locks and evidence under
`<target>/.edad/`. This repo holds only the harness and its own tests.

## Install

Into the target repo's environment:

    pip install -r requirements-gate.txt      # the gate's pins, exact
    export PYTHONPATH=/path/to/this/repo      # until the package is installable

The target repo needs, at its root:

- `requirements-gate.txt` — a copy of this repo's. The gate refuses to run on
  mismatched pins: its verdict must be a property of the commit, not of
  whatever was installed that day.
- `.edad/tickets/`, `.edad/grills/`, `.edad/specs/` — the harness creates
  only the directories it writes to itself.
- `.gitignore` entries for `.edad/records/ sessions/ worktrees/ runs/`.
  Evidence and hashes are deliberately committed — see this repo's
  `.gitignore` for the reasoning.
- `.claude/skills/` — copy this repo's. The method ships with the harness.
- If `ruff check .` is going in `full_gate`: a pinned `[tool.ruff.lint]
  select` in its pyproject, for the same reason as the pins.

## Validate the rig before any agent touches it

Write one throwaway ticket, `T001`, against something small and pure in the
target, with a `mutation:` block (brownfield: the test is green on day one,
so the proof is that it dies under a named edit). Then, from the target root:

    python3 -m edad.gate approve T001     # hashes the frozen tests; runs the proof
    python3 -m edad.gate run T001         # => PASS

Now break each check by hand and confirm the gate sees it:

| scenario                          | freeze | scope | commands |
|-----------------------------------|--------|-------|----------|
| correct, in scope                 | PASS   | PASS  | PASS     |
| acceptance not yet met            | PASS   | PASS  | FAIL     |
| frozen test edited                | FAIL   | -     | not run  |
| touched files outside `scope`     | PASS   | FAIL  | PASS     |

Row three is the whole thesis: a tampered acceptance test is never executed,
so a passing suite cannot launder a weakened contract. Delete the ticket
afterwards; the rig is validated against *that* tree.

## Run

    python3 -m edad.session run T00N                        # host tier, you watch
    python3 -m edad.session run T00N --sandbox docker       # container, unattended
    python3 -m edad.session_queue run T00N T00M ...         # a night's queue

The docker tier needs `Dockerfile.agent` built as `edad-agent:latest` plus
the target's own test dependencies layered on top (`--image` names the
result), and `Dockerfile.egress` as `edad-egress:latest`. Inside the
container the agent runs with permissions bypassed — the container is the
boundary. On the host it is allowed exactly the ticket's own gate commands.

## Evidence

Every run writes `.edad/records/<ticket>-<ts>-<sha>.json`: commit, per-check
verdict, per-command exit code, duration, output tail. That file is the
record. The agent's own summary of its work is not an input to it. When a
ticket passes its full gate the controller promotes the record to
`.edad/evidence/<ticket>.json`, committed beside the code it verifies.

## Where to read next

- `edad/gate.py` — approve, freeze, scope, red and mutation proofs, the
  full_gate baseline ratchet
- `edad/session.py` — worktree + container + `claude -p`, kill conditions,
  gate after every iteration
- `edad/session_queue.py` — many tickets on one run branch, overnight
- `.claude/skills/` — find-seams / grill-me / to-spec / to-tickets / handoff
- `.edad/` — this harness was built with itself; T001–T017 are the record
