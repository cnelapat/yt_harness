# EDAD rig — quickstart

Bootstrap repo for the gate runner. The YouTube scripts are the fixture, not
the point: they give you a red commit and a green commit you control, so you
can prove the harness works before any agent touches it.

    pip install pyyaml pytest
    git init && git add -A && git commit -m "red: T001 acceptance"

    python3 -m edad.gate approve T001      # hash the frozen acceptance tests
    python3 -m edad.gate run T001          # => FAIL (nothing implemented)

Write `ytmp3/converter.py` by hand until it passes, then:

    python3 -m edad.gate run T001 --base-ref HEAD   # => PASS

Delete your implementation. Now the rig is validated and an agent can try.

## The four states the gate must distinguish

| scenario                          | freeze | scope | commands |
|-----------------------------------|--------|-------|----------|
| nothing implemented               | PASS   | PASS  | FAIL     |
| correct, in scope                 | PASS   | PASS  | PASS     |
| acceptance test edited            | FAIL   | -     | not run  |
| touched files outside `scope`     | PASS   | FAIL  | PASS     |

Row three is the whole thesis: a tampered acceptance test is never executed,
so a passing suite cannot launder a weakened contract.

## Evidence

Every run writes `.edad/records/<ticket>-<ts>-<sha>.json`: commit, per-check
verdict, per-command exit code, duration, output tail. That file is the record.
The agent's own summary of its work is not an input to it.

## Next

- `edad/session.py` — worktree + container + `claude -p`, kill conditions,
  gate after every iteration
- `.claude/skills/` — grill / to-spec / to-tickets
