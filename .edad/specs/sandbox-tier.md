---
slug: sandbox-tier
grilled: .edad/grills/sandbox-tier.md
status: draft
---

## Problem statement

`--sandbox docker` is the harness's claim that an unattended agent is contained. T002
built a `--network` tier on top of it so a contained agent could still reach a fixture
the operator provides, and wired it end to end in the single-ticket path. It shipped
clean, with `spec: null`, and two things were then true that nobody wrote down.

The unattended path cannot reach it. The multi-ticket controller spawns each session as
`python3 -m edad.session run <id>` with no `--sandbox` and no `--network`, and the
session defaults to `none`. So the overnight run — the one path where nobody is watching
the agent — is the one path that never sandboxes. Its spec never uses the words
"sandbox" or "network"; this was not decided against, it was never raised.

And the tier has never run. No `edad-agent:latest` image exists on this machine; all 22
session logs record `sandbox: none`; every T002 test is a monkeypatched probe or a pure
argv builder. Measured against live docker during the grill, the tier turned out to be
non-functional rather than merely unreachable: a container on `--network none` or on an
internal network has no route to `api.anthropic.com`, so `claude -p` inside it cannot
reach the model at all; `docker run -e CLAUDE_CODE_OAUTH_TOKEN` with the variable unset
passes nothing and raises no error, so an operator who never minted a token gets a
container with no credential and no warning; and T002's own out-of-scope note — that a
fixture on the internal network publishes a host port for the verifier — describes
something docker cannot do.

Every one of these failures is invisible for the same reason: they all surface as the
same symptom, a 15-retry storm from the CLI that ends at `AGENT_TIMEOUT_S`, indistinguishable
from an agent that is merely slow. That is why so many decisions below say "probe it at
plan time" rather than "document it".

## Solution

A docker night is one that can actually run, and says so before it starts.

The queue carries the tier: `session_queue run --sandbox docker --network <name>` threads
both flags to every child, defaults to `none` so nothing about tonight changes silently,
and records the tier in the run log so the morning can answer "did this night run
sandboxed". Every child's recorded command states its tier rather than inheriting it.

The agent reaches the model and nothing else. A harness-owned proxy container, dual-homed
bridge-first and then attached to the internal network, forwards `CONNECT` to
`api.anthropic.com` and refuses every other target before any TLS handshake. The agent
container stays on the internal network with zero egress of its own, is pointed at the
proxy, and has telemetry disabled so the allowlist is one entry. The proxy is small
enough to read in one screen because the allowlist *is* the security boundary. Whoever
created it destroys it: the queue ensures one before the run and removes it after; a
standalone session creates and removes its own; a session that finds one leaves it.

Preflight measures the configuration the agent will actually run in, at plan time,
before anything executes. A network that is not internal, or one from which a throwaway
container can reach the internet unconfigured, is refused. A proxy that permits a
non-allowlisted host, or that refuses the model API, is refused — and the permit probe
uses the real token the container will receive, so one call proves the proxy permits the
API, the token is valid, and it is the token the agent will hold. `--sandbox docker`
without `--network`, which can never run a real agent, is refused with a message saying
so; so is `--sandbox docker` without `CLAUDE_CODE_OAUTH_TOKEN`, naming `claude setup-token`.
A session that aborts in preflight writes no log, and the queue now treats N consecutive
logless sessions as an environment failure and stops the night, rather than resetting
its breaker and draining the queue.

The fixture contract is corrected to the one ordering docker supports — start on bridge
with `-p`, then `docker network connect` the internal net — and the harness still
creates no fixtures. `network_access: deny` is documented as enforced on the agent side
and advisory on the verifier side, and each run records which half was enforced. And
today's hand probes become a repeatable, opt-in container property test that proves the
load-bearing claims against real docker without needing an API key.

## User stories

1. As an operator, I want to pass `--sandbox docker --network <name>` once to the queue
   and have every child session run under it, so that one decision per night covers the
   whole run.
2. As an operator, I want the queue to default to `--sandbox none`, so that a night I
   did not think about does not silently start exercising a code path that has never
   executed.
3. As an operator, I want the run log and every child's recorded command to state the
   tier, so that in the morning "did this night run sandboxed" is a lookup rather than an
   inference from two files' defaults.
4. As an operator, I want a missing or non-internal network refused at plan time, before
   the branch is cut, so that a misnamed network costs seconds rather than a night.
5. As an operator, I want the night stopped when consecutive sessions produce no log at
   all, so that a fixture network torn down at ticket 3 of 12 does not drain tickets 4–12
   as instant aborts that the run log reports as "ran out of tickets".
6. As an operator, I want the agent inside the sandbox to reach the model API, so that
   `--sandbox docker` is a tier that runs rather than a tier that is documented.
7. As an operator, I want the agent to reach *only* the model API, so that the
   unattended overnight agent is not handed the open internet — the thing this grill
   started from.
8. As an auditor, I want the egress allowlist to be one entry in one small file that the
   harness builds and owns, so that the security boundary is readable at a glance and
   cannot be widened by an operator-supplied config I never saw.
9. As an operator, I want preflight to prove the proxy refuses a non-allowlisted host
   *and* permits the model API with the token the agent will hold, so that a dead proxy,
   a stale image, a name collision or an expired token is a plan-time refusal with a
   reason rather than a 15-retry storm at 3am.
10. As an operator, I want `--sandbox docker` without `--network` refused with a message
    saying it can never run a real agent, so that the obvious invocation is not the
    broken one.
11. As an operator, I want `--sandbox docker` without `CLAUDE_CODE_OAUTH_TOKEN` refused
    naming `claude setup-token`, so that "unset" gets a better message than a failed API
    call — and I want the token's value never to appear in argv, so it cannot leak into
    `ps` or a logged command line.
12. As an operator, I want the proxy created once per night and torn down by whoever
    created it, so that a queue does not pay N container starts and a standalone session
    does not leave a proxy behind.
13. As an operator, I want the fixture recipe I am given to be one docker actually
    supports, so that the wrong start order does not silently produce a service the
    verifier cannot reach.
14. As an auditor, I want `network_access: deny` documented as enforced on the agent
    container and advisory for the verifier, and each run to record which half was
    enforced, so that the ticket schema does not claim more than the harness measures.
15. As a maintainer, I want the docker-tier claims — no egress on `--network none`, by-name
    reach plus no egress on the internal net, a bridge-first fixture reachable from the
    host, the real proxy image permitting only the model API — proved by a repeatable
    test I opt into, so that the tier is verified as a sandbox and not as a string
    builder, and so that the test bites when run rather than auto-skipping forever.

## Seams

Nine, named below. Six exist. The two new code seams are both in the proxy, which is
new by construction; the third new seam is the hand-run container property test, which
is a seam in the sense that it observes real docker and not in the sense of being a
ticket's acceptance command (D12). Where a decision spans seams its `seam:` field lists
each, and the Decisions preamble says how a spanning decision is sliced across tickets.

- **`queue-builders`**
  - **Where**: the controller's pure builders — `session_argv()` and `build_parser()`
    in `edad/session_queue.py`.
  - **Exists**: yes. Both are already frozen-tested by exact equality / parse result.
  - **Observes**: that `--sandbox <tier>` is in every child's argv, including the
    default; that `--network <name>` is emitted exactly once when given and not at all
    when not; that the queue parser accepts `--sandbox {none,docker}` defaulting to
    `none` and `--network NAME` defaulting to `None`.
  - **Discharges**: D1, D4 (the default half), D6.
  - Two frozen stand-ins are amended **before approval**. `tests/test_session_queue.py:296`
    pins `session_argv("T004")` by exact list equality (D6). And `FakeSession.__call__`
    at `:135` is what every queue test substitutes for `run_session`, with the arity
    `(root, ticket_id)` — so the tier must reach the child either as a keyword-defaulted
    argument the fake tolerates, or the fake is amended. `breaker_fired`'s call sites are
    keyword-only, so the `queue-breaker` seam below needs no amendment.

- **`queue-plan`**
  - **Where**: `run_queue()` between `plan_run` and the branch cut, with the network
    validation and the proxy lifecycle reached through names imported from `edad.session`
    and `edad.egress` so a test replaces them at the module and observes the order.
  - **Exists**: yes — `run_queue` already computes the plan before anything executes
    (controller D1). What this adds is two more things that happen there.
  - **Observes**: that a docker night ensures the proxy and then validates the network
    before the run branch is cut and before any session is spawned; that a refused
    network refuses the run at ticket 0 with no branch cut; that the queue removes the
    proxy after the run only if it created it; that a `none` night asks docker nothing.
  - **Discharges**: D5 (the plan-time half), D17 (the queue half).

- **`queue-breaker`**
  - **Where**: `breaker_fired()` and `RunState.fail()` in `edad/session_queue.py`.
  - **Exists**: yes. `breaker_fired` is pure and keyword-only; `fail(session_log=None)`
    is the path a logless session already takes.
  - **Observes**: that N consecutive logless sessions fire a third breaker, named
    distinctly from `no_progress` and `wall_clock`; that a logless session still
    *resets* `no_commit_aborts` (the existing case stays correct); that a session with a
    log resets the logless count; that the firing is recorded in the run log as a
    breaker, not a ticket failure.
  - **Discharges**: D5 (the breaker half).

- **`logs`**
  - **Where**: `.edad/runs/<ts>.json` (the run log) and `.edad/sessions/<id>-<ts>.json`
    (the session log), both on disk, both already gitignored telemetry.
  - **Exists**: yes. Both files and both `as_log()` / `asdict(SessionLog)` shapes exist.
  - **Observes**: that the run log carries `sandbox` and `network` at the top level;
    that the session log carries `network` beside its existing `sandbox` and records
    which half of `network_access: deny` was enforced — the agent container, mechanically,
    or the verifier, advisorily via `EDAD_NETWORK`.
  - **Discharges**: D4 (the log half), D8 (the recording half).

- **`session-preflight`**
  - **Where**: `validate_network()` in `edad/session.py`, with every shell-out at a named
    module-level function the way `docker_network_internal` already is, so each answer
    can be stubbed without a daemon.
  - **Exists**: yes. `validate_network` runs last in `preflight` and already owns the
    `Internal: true` refusal. It grows to own every docker-tier refusal, so that the
    queue calling it once at plan time (D5) and the session calling it in `preflight`
    get identical checks from one function.
  - **Observes**: refusal of `--sandbox docker` with no `--network`; refusal of a network
    on which an unconfigured throwaway container reaches the internet, and of one where
    that probe could not be run; refusal when the proxy permits a non-allowlisted host;
    refusal when the proxy refuses `api.anthropic.com`; that the permit probe is made
    with the `CLAUDE_CODE_OAUTH_TOKEN` in the environment and in the agent image on the
    named network; refusal when that variable is unset, naming `claude setup-token`; and
    the order — the plain refusals first, then the network's own isolation, then the
    proxy.
  - **Discharges**: D10, D11, D16, D18.
  - **What each probe is, measured** (grill record, third pass). D11's egress probe is a
    raw-IP TCP connect from an unconfigured container on the network: DNS-free, and
    instant in both directions (`Network is unreachable` in 0.00s on an internal net;
    reached in 0.06s on bridge). D16's refusal probe is a `CONNECT` to a non-allowlisted
    host from a container configured for the proxy, expecting the proxy's 403 (0.01s, no
    upstream attempt). D16's permit probe is `claude -p` itself, `--max-turns 1`, in the
    agent image, on the named network, with the real token: the client the agent will
    use, 4s and exit 0 when everything is right, 3s and a 401 on a bad token, 20s of
    retries and a 403 on a dead proxy. The two failure modes print the same
    `Failed to authenticate` prefix and are told apart by status and shape, which is why
    the check is two probes and not one.
  - **Preflight gains a side effect.** The proxy must exist before it can be probed, so
    a standalone session ensures it before `validate_network` runs — and a refusal for
    any later reason has then already created a container. Teardown belongs in a
    `finally` around the whole session, not at its end.
  - The existing `test_the_default_tier_asks_docker_nothing` asserts that
    `validate_network("docker", None)` is accepted, which is the combination D10 refuses;
    it is amended **before approval**. `test_an_internal_network_is_accepted` stubs only
    the internal probe and asserts `calls == [NAME]`; once the egress and proxy probes are
    reached through their own named functions it must stub those too or it will shell out
    to docker under the test. Both amendments are pre-approval and are the ones the grill
    priced into D10; the second is named here because it was not.

- **`session-builders`**
  - **Where**: `agent_argv()` and `network_rule()` in `edad/session.py`, both pure and
    both already frozen-tested.
  - **Exists**: yes.
  - **Observes**: that the docker argv sets `HTTPS_PROXY`/`HTTP_PROXY` to the proxy's
    deterministic name on the internal network; that it sets
    `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; that it still passes the token as the
    bare `-e CLAUDE_CODE_OAUTH_TOKEN` form and the value is never in argv even when the
    variable is set; that the named tier's prompt sentence says egress is the model API
    via the proxy and nothing else.
  - **D19, added 2026-09-11**: that the docker argv carries
    `--dangerously-skip-permissions` and no `--permission-mode`; that the host argv
    carries `--permission-mode acceptEdits` and `--allowedTools` built from the ticket's
    commands, with the prompt ahead of it; that `--yolo` on the host bypasses; that
    `allowed_tools()` is pure and `SessionLog` carries `permissions`. Its frozen file is
    new, `tests/test_session_permissions.py`, so T016's frozen file is untouched.
  - **Discharges**: D9, D15 (the container-env half), D18 (the argv half), D17 (the
    session half — a standalone session creates and removes its own proxy, leaves one it
    found — is observed at `cmd_run`, reached through the same imported names as
    `queue-plan`).
  - The frozen assertions for the named-tier sentence
    (`tests/test_session_network.py:156-164`) are loose enough that D9's prose change
    needs no amendment; the new assertion is added beside them.

- **`proxy-daemon`** — new
  - **Where**: the `CONNECT`-allowlist daemon, `edad/egress_proxy.py`, run in-process
    against a loopback listener with a local stand-in for the upstream. Stdlib only, one
    screen, runs as `python3 -m edad.egress_proxy` inside `Dockerfile.egress`.
  - **Exists**: new. The measurement it rests on is in the grill record: the CLI sends
    `CONNECT api.anthropic.com:443` in plaintext before TLS, so the host is matched
    before any handshake and nothing is decrypted.
  - **Observes**: that a `CONNECT` to the allowlisted host is spliced to the upstream;
    that a `CONNECT` to any other host, a raw IP, or a non-`CONNECT` method is refused
    with no upstream connection attempted; that the allowlist is exactly
    `{"api.anthropic.com"}`.
  - **Discharges**: D14, D15 (the allowlist half).

- **`proxy-lifecycle`** — new
  - **Where**: `edad/egress.py` — `proxy_name(network)`, `ensure_egress_proxy(network)`,
    `remove_egress_proxy(network)` — with the docker shell-out at one named function so
    the argv sequence is observable without a daemon.
  - **Exists**: new. The dual-homing recipe it encodes is the one D2 gives operators for
    fixtures.
  - **Observes**: the deterministic name `edad-egress-<network>`; that `ensure` starts
    the container on bridge first and then `docker network connect`s the internal net;
    that `ensure` creates only when absent and returns whether it did; that `remove`
    tears down by name; that the image is the one built from the checked-in daemon.
  - **Discharges**: D13, D17 (the lifecycle half).

- **`docker-property`** — new, hand-run, not a ticket
  - **Where**: `docker_tests/test_properties.py`, **outside** `pyproject.toml`'s
    `testpaths`, run by explicit path with `EDAD_DOCKER_TESTS=1` set — the module raises
    at import when the variable is unset. Against live docker, no API key needed.
  - **Exists**: new. Its contents are the grill's probe table made repeatable.
  - **Observes**: a container on `--network none` has no egress; a container on the
    internal net reaches a fixture by name and still has no egress; a fixture published
    bridge-first and then attached is reachable from the host *and* by name; a fixture
    started on the internal net cannot publish a host port; the real proxy image, dual-
    homed, permits `CONNECT api.anthropic.com:443` and refuses another host.
  - **Discharges**: D2, D7, and the live half of D14.
  - Written by hand, per D12: its deliverable is a test file, which inverts the
    `frozen`-as-spec / `scope`-as-implementation roles, and there is no valid EDAD shape
    for it. It lives outside `testpaths` because every ticket's `full_gate` runs
    `python3 -m pytest -q`, and a gated file inside `tests/` has only three behaviours
    when the variable is unset: fail (every full gate red forever — unwinnable), skip
    (the auto-skip D7 rejects), or not be collected. Only the third is acceptable, and
    it is a property of where the file sits, not of what it contains.

**Decisions with no seam**: D3 and D12. D3 is discharged by this file existing — the
corrections landed in a new spec and `T002.md` is untouched. D12 is the cut itself, a
structural decision with no behaviour to attach a test to. Both carry `unenforced:`.

## Implementation decisions

**The queue threads the tier and nothing else.** `session_queue run` gains `--sandbox`
and `--network` with the session's own choices and defaults, and `session_argv` emits
`--sandbox` in every branch so the child's recorded command states its tier rather than
inheriting it — two defaults in two files cannot then drift apart. `--image` and
`--yolo` are not threaded; that is an open item, not an oversight, and is re-raised
below. The run log carries the tier at the top level, beside `run_branch`.

**One validation function, two callers.** `validate_network` keeps its `Internal: true`
check and grows to own every docker-tier refusal: docker without a network, no token,
measured egress from the network, a proxy that permits too much or too little. The queue
calls it once at plan time, after ensuring the proxy and before cutting the branch; the
session calls it from `preflight` as it always has. Keeping the refusals in one function
is what makes "the queue validates at plan time" the same claim as "the session
validates", rather than two lists that agree today.

**Refuse before the log exists, and count that.** `preflight` runs before `SessionLog`
is constructed, so a preflight refusal exits 2 with no log — which is correct for the
session and was invisible to the queue, whose breaker reset on a missing log. The queue
gains a third breaker for N consecutive logless sessions. The existing reset of the
no-progress count on a missing log stays: a logless session is distinguishable evidence,
not absent evidence, and the two counters answer different questions.

**The proxy is the boundary, so the harness builds it.** A purpose-built daemon reads
the `CONNECT` line, matches the host against a one-entry allowlist, and either splices
bytes or refuses — no interception, no certificate, no decryption, no config language
with filter semantics. It lives in its own module, stdlib only, small enough that the
allowlist can be audited by reading. Its image is built from that checked-in file. The
lifecycle — deterministic name per network, dual-homed bridge-first, ensure-if-absent,
teardown-by-creator — lives in a second small module both entry points import, so the
queue creates one per night and each child finds it present, and a standalone session
creates and removes its own with no new flag. This narrows T002's "the harness creates
nothing" to fixtures, which stay the operator's because they are arbitrary and
ticket-specific.

**The agent is pointed at the proxy by environment.** The docker argv sets
`HTTPS_PROXY` and `HTTP_PROXY` to the proxy's name on the internal network and disables
non-essential CLI traffic so the allowlist is one host. The token still crosses as a
bare `-e CLAUDE_CODE_OAUTH_TOKEN` pass-through: the value never appears in argv, so it
cannot reach `ps` or a logged command line. Residual exposure inside the container is
accepted — the agent needs the credential to function.

**Preflight measures what will run, not what is installed.** The egress probe starts a
throwaway container on the network that attempts a raw-IP TCP connect — no DNS involved,
instant either way — and refuses if it reaches out, or if the probe could not be run at
all, parity with the existing rule that an unmeasured network is refused whatever the
reason. The proxy is probed in both directions: a `CONNECT` to a non-allowlisted host
must come back refused, and `claude -p` with one turn, in the agent image, on the named
network, with the real token, must exit 0. That one call is the client the agent will
use and is the only check that catches an expired token, a stale or missing agent image,
a name collision, or a hand-started proxy. Measured: 4s when everything is right, 3s to
a clean 401 on a bad token, 20s of retries to a 403 on a dead proxy. Its cost — one
trivial model turn per validation — is accepted; a child session's own `preflight`
repeats it, so a docker night of N tickets makes N+1 such calls, not one.

**The prompt says what is measured.** The named tier's sentence becomes "no internet
egress except the model API, via the proxy", which is exactly what preflight has just
proved. `network_access: deny` is documented as enforced on the agent container and
advisory on the verifier, and the session log records which half was enforced.

**The container property test is the tier's proof of being a sandbox.** Everything above
is verified as a string builder and a set of stubbed refusals, which is what a frozen
test without a daemon can do. The property test is the one place the claims meet real
docker, and it must be opted into so that it never passes by skipping.

**Ordering, settled here by the operator.** The grill left open whether the cut runs
T013→T016 or leads with T015/T016. It leads with T015/T016. The reason the grill gave —
nothing about a night changes until those two ship — turned out during the seam pass to
be a dependency rather than a preference: T013's plan-time validation *calls* T016's
`validate_network` and T015's proxy lifecycle, so it cannot be authored against a tree
that lacks them. The order is T015 → T016 → T013, with T014 independent of all three.

## Out of scope

- **Verifier-side network enforcement.** D8 makes the current state honest and records
  it; it does not enforce it. Named as a future decision, not scheduled.
- **Egress filtering finer than one allowlisted host.** Deliberately, for the first time.
  D14 rejects a DNS allowlist because DNS does not bind the connection; anything finer
  than a `CONNECT`-target match is a different mechanism.
- **Fixture lifecycle.** The harness creates and destroys the proxy (D13) and nothing
  else; fixtures are the operator's, started per D2's recipe. Rejected in the grill as
  compose-shaped lifecycle in a harness that shells out to docker once.
- **Joining the verifier to the network.** Would remove the dual-homing but contradicts
  `run_commands()`-on-host across the gate's call sites. Rejected in the grill.
- **Per-ticket network declaration in frontmatter.** One tier per night (D1). Rejected in
  the grill: it puts a security-relevant field inside the approval hash.
- **Editing `T002.md`.** Its approval lock checks its bytes at every session start; D3
  lands the corrections here instead.
- **Token minting, storage and rotation.** D16 turns expiry into a plan-time refusal,
  which is detection. Who mints the token and where it lives is not designed here.
- **`--image` and `--yolo` on the queue.** D1 threads `--sandbox` and `--network` only.
  See Further notes.

## Further notes

**Module names and the port are spec-stage resolutions, not grilled decisions.** The
grill names no paths for the proxy. Settled here so `to-tickets` has a `scope`:
`edad/egress_proxy.py` for the daemon, `edad/egress.py` for the lifecycle,
`Dockerfile.egress` for the image, `tests/test_egress_proxy.py` and
`docker_tests/test_properties.py` for the tests, and one constant for the proxy port.
None carries a `D` id. If a name matters, re-grill it rather than inherit it from here.
The daemon and the lifecycle are two files rather than one because the daemon is the
security boundary and should be readable without the docker plumbing beside it.

**The logless breaker's N is not settled.** D5 says "N consecutive" and does not pick N.
The spec-stage default is `MAX_NO_PROGRESS`, the session's own threshold that the
no-progress breaker already reads one level up, for the same reason that breaker uses
it: one absorbs a transient, two in a row is systematic. Confirm or change before T014
is approved; it is one constant.

**A missing proxy image is refused, not built.** D13 says the harness owns the proxy and
D16 says a stale image is caught by the probe; neither says what `ensure` does when the
image is absent. Spec-stage default: refuse, naming the build command — parity with the
agent image, which the harness also does not build, and with D18's shape, where "unset"
gets a plain message rather than a downstream failure. A missing *agent* image is caught
by D16's permit probe, which runs in that image, and surfaces as docker's own "no such
image" — the spec-stage reason the probe runs in the agent image rather than a generic
one.

**Per-ticket re-validation happens by construction.** The grill rejected re-validating
the network before each ticket as a subprocess in the queue's hot loop. The child
session's own `preflight` re-runs `validate_network` anyway, so a docker night probes
egress and the proxy N+1 times. That is not the rejected design — it is the session
being the session — and it has a benefit the rejection wanted: a fixture network torn
down at ticket 3 is caught at ticket 4 by the session, and the logless breaker (D5) then
stops the night. The cost is one container start and one authenticated call per ticket.

**The documentation half of D8 is a hand edit.** "`network_access: deny` is enforced on
the agent container and advisory for the verifier" belongs in the to-tickets skill's
line on `network_access` and in `Dockerfile.agent`'s comment, which already half-says it.
Neither is a sensible `scope` entry for an agent; it lands by hand alongside the D7 test.
The recording half is verified.

**Spanning decisions and the amended stand-ins.** D5, D15, D17 and D18 each have verify
commands in more than one frozen file; the Decisions preamble says how they slice. Two
frozen files are amended before approval: in `tests/test_session_queue.py`, the exact
argv at `:296` (D6) and `FakeSession.__call__`'s arity at `:135` if the tier is passed
to `run_session` positionally; and in `tests/test_session_network.py`,
`test_the_default_tier_asks_docker_nothing` (D10) and
`test_an_internal_network_is_accepted` (D11's new probes must be stubbed). Amending a
frozen stand-in that pins the wrong arity is the documented pre-approval procedure, not
a violation of it. Because T013 and T014 both freeze `tests/test_session_queue.py`, and
the queue's just-in-time re-approval refuses on a frozen-hash change, the two are
authored and approved one at a time as T004–T006 were.

**Measured after the spec was drafted.** The grill's third pass ran the whole path end
to end on 2026-09-11: `edad-agent:latest` now exists on this machine, and a valid
`setup-token` through a one-entry proxy from a DNS-less internal net produced `ok` in 4s
dialling `api.anthropic.com` alone. The tier is no longer a design that has never run;
it is a design that has run once, by hand, through a prototype of T015's daemon.

**`--dry-run` under the docker tier is not settled.** Today it skips the `claude`/docker
checks but still calls `validate_network`. With the probes added, a docker dry-run
would start a proxy and spend a model turn to print a prompt. The spec-stage default is
that dry-run runs the isolation check only and says the probes were skipped — but that
is a call, not a derivation, and is listed under `deferred:`.

**The proxy's restart policy, settled 2026-09-11 by the operator after the tier's
first night: none, and the deferred line that asked for one overstated the exposure.**
A proxy that dies mid-night is not caught "after up to ~an hour" by the queue's
breakers — it is `--rm`'d away, so the next child session's own `ensure_egress_proxy`
finds it absent and starts a fresh one, and `preflight` permit-probes it. The
between-ticket health check the line offered as the alternative therefore exists by
construction. The only exposure is mid-ticket: the agent's CLI storms against a dead
proxy for `AGENT_TIMEOUT_S`, twice, until `MAX_NO_PROGRESS` aborts the session — about
30 minutes and one ticket left undone, after which the night continues. Accepted. The
only in-daemon crash path is an unguarded `OSError` from `serve()`'s `accept()`, which
needs fd exhaustion that one agent's traffic cannot produce; everything else is the host
killing the container, which no docker flag survives. Rejected: `--restart on-failure`,
because it conflicts with `--rm` and reopens the reason `--rm` was chosen; re-ensuring
between iterations, a halving for the same non-failure; hardening the accept loop, a
change to the boundary's one-screen file. Re-open on the first session log that shows a
dead proxy.

**The agent's permissions, settled 2026-09-11 by the operator after the tier's first
night (D19).** The grill's worry was right and understated. `--permission-mode
acceptEdits` under `claude -p` has nobody to answer a prompt, so every Bash call is
denied — in the container *and* on the host. T014's agent, in docker, ended with "I
could not actually run the three acceptance tests, the full frozen suite, or ruff
myself"; T013's, on the host, "traced each of the ten tests against the diff by hand".
Every ticket T001–T016 passed its gate on an agent that never ran a test. Nothing is
re-run: the gate is the verdict, not the agent, and this is that thesis holding under a
condition nobody intended. But a one-iteration pass on a small ticket hides a blind
agent, and a multi-iteration brownfield refactor will not.

The decision is one rule per tier, because the two tiers have different boundaries.
Under `--sandbox docker` the container *is* the boundary — model-API-only egress (D14,
D15), the worktree as the only mount, and a `.git` file that points at a host path so git
inside cannot rewrite anything — and `acceptEdits` there was not a safeguard but a
crippled agent. The CLI runs with `--dangerously-skip-permissions`, unconditionally.
Measured: the CLI refuses that flag as root ("cannot be used with root/sudo privileges")
and `Dockerfile.agent` has no `USER` line, so the image gains a non-root user with a
writable `HOME`; `IS_SANDBOX=1`, which the CLI also honours, is rejected as an
undocumented escape hatch where a `USER` line is the honest fix. On the host there is no
boundary. The agent keeps `acceptEdits` and gets `--allowedTools` naming the ticket's own
gate commands: each `acceptance` and `full_gate` command exactly, as `Bash(<command>)`,
plus `Bash(<tokens through pytest>:*)` for every command that invokes pytest, so it can
run one node id. Measured on the host: an unlisted `python3 -c` and `curl` are denied, a
listed `echo` runs — and `--allowedTools` is variadic, so the prompt must precede it in
argv or the CLI reports no prompt was given. Two limits, stated: the operator's own
`~/.claude/settings.json` allow rules apply to a host-tier agent as well, so the
allowlist is a floor and not a ceiling; and a gate command that is itself a shell
(`bash -c "..."`) hands the agent a shell, which is an authoring rule for `to-tickets`
rather than something the builder can detect. `--yolo` stays the host-tier opt-in for
bypass and does nothing under docker, where bypass is already the rule. The session log
records `permissions: {mode, allowed}` beside `network_access`, so a log says what the
agent could *do* and not only where it could reach. Rejected: passing `--yolo` through
the queue, where every night without the flag silently repeats the blind agent; and an
allowlist inside the container, which forbids `ls`, `grep` and a scratch script for no
gain the container is not already giving.

**Carried open from the grill, unchanged.**
- `--image` pass-through on the queue. (`--yolo` was carried here too; measured after the
  tier's first night and settled as D19, below — the agent could not run its checks, in
  either tier.)
- Token minting and rotation (above, under Out of scope).
- What happens when the CLI's required hosts change. D15 pins one measured entry; a
  future release needing a second host fails as the retry storm, caught by D16 at plan
  time only if the probe is re-run against the new CLI. D7's property test is the natural
  home for re-measuring and does not currently do it.

**The stated assumption D15 rests on** — the container holds a long-lived `setup-token`
and performs no refresh or authorize flow, so `platform.claude.com` is deliberately not
on the allowlist — is carried in D15's own text. Widening the allowlist is not the fix
for an expired token; D16 is.

## Decisions

Carried from `.edad/grills/sandbox-tier.md`, **with a difference from the two earlier
specs that is recorded here rather than left to be assumed from the heading.** That grill
record carries its decisions as prose paragraphs and no yaml block: it has no `verify:`
commands, no `frozen:` and no `scope:` to copy. So three things in the block below are
authored at this stage, not transcribed:

- Each `decision:` line is the grill entry's bold headline plus its operative sentence,
  condensed to one line. **The grill's paragraph is authoritative where the two differ.**
- Every `verify:` command and its test node id is authored here from the Seams section.
  The tests do not exist yet; `to-tickets` writes them and watches them go red.
- `frozen:` and `scope:` are taken from the grill's `## The cut` where it names them
  (T013, T014, T016) and from the spec-stage resolutions in Further notes where it does
  not (T015, the property test).

Ids are the grill's, never renumbered. They are scoped to this spec, as
`.edad/specs/mutation-proof.md`'s D1–D23 are to it; a ticket's `decisions:` resolve
against its `spec:`.

**Slicing a spanning decision.** D5, D15, D17 and D18 list verify commands in more than
one frozen file. Each ticket takes the commands whose frozen file it owns and lists the
decision id; the evidence records then say, per ticket, which half of the decision that
ticket's gate proved. The ticket each command belongs to is stated in a comment beside
it.

```yaml
decisions:
  - id: D1
    decision: "The queue carries the tier: `session_queue run` gains `--sandbox` and `--network`, threaded through `session_argv()` to every child session — one decision per night, uniform across the queue, keeping T002's operator-supplies-the-network model."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_the_queue_parser_takes_sandbox_and_network -q
      - python3 -m pytest tests/test_session_queue.py::test_session_argv_carries_the_network_to_every_child -q
    frozen:
      - tests/test_session_queue.py
    scope:
      - edad/session_queue.py
    seam: queue-builders
    rejected: "per-ticket network declaration in frontmatter — right only if tickets need different services, makes the single-ticket path honour the field too, and puts a security-relevant field inside the approval hash"

  - id: D2
    decision: "The fixture contract is dual-homed and the harness still creates nothing: the operator starts the fixture on bridge with `-p`, then `docker network connect <internal> <fixture>`; the verifier stays on the host and reaches it at `127.0.0.1:PORT`; `run_commands()` is untouched."
    verify:                                     # hand-run, not a ticket — see D12
      - EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py::test_a_fixture_published_bridge_first_is_reachable_from_the_host_and_by_name -q
      - EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py::test_a_fixture_on_the_internal_net_alone_cannot_publish_a_host_port -q
    frozen:
      - docker_tests/test_properties.py
    seam: docker-property
    rejected: "harness manages the fixture lifecycle — removes a real footgun but T002 excluded creation/teardown and it drags compose-shaped lifecycle into a harness that shells out to docker once; and joining the verifier to the network, which contradicts run_commands()-on-host across gate.py's call sites"

  - id: D3
    decision: "Corrections land in a new spec, `.edad/specs/sandbox-tier.md`, retro-covering T002's tier; the new tickets cite it via `spec:` + `decisions:`. `T002.md` is not edited."
    unenforced: "Discharged by this file existing and `T002.md` being untouched. Editing `T002.md` would fail its `ticket_sha256` check at every session start and rewrite the record of what was approved when its evidence was earned."
    seam: none
    rejected: "editing T002.md in place — breaks its approval lock"

  - id: D4
    decision: "The queue defaults to `--sandbox none`, parity with `session.py`, so nothing about tonight changes silently; the run log gains the tier so the morning can answer whether the night ran sandboxed."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_the_queue_parser_defaults_to_no_sandbox -q
      - python3 -m pytest tests/test_session_queue.py::test_the_run_log_records_the_tier -q
    frozen:
      - tests/test_session_queue.py
    scope:
      - edad/session_queue.py
    seam: queue-builders, logs
    rejected: "defaulting to docker — promotes a default to a code path that has never executed; and requiring an explicit --sandbox, which breaks every existing invocation and gets aliased away in a week"

  - id: D5
    decision: "Validate up front and add a logless-session breaker: `validate_network()` runs once at plan time before anything executes, so a missing network refuses the run at ticket 0; and N consecutive sessions that produced no session log at all stop the night as an environment failure, while a logless session still resets the no-progress count."
    verify:
      # T013 — the plan-time half
      - python3 -m pytest tests/test_session_queue.py::test_a_docker_night_validates_the_network_before_the_branch_is_cut -q
      - python3 -m pytest tests/test_session_queue.py::test_a_refused_network_stops_the_run_at_ticket_zero -q
      # T014 — the breaker half
      - python3 -m pytest tests/test_session_queue.py::test_consecutive_logless_sessions_stop_the_night -q
      - python3 -m pytest tests/test_session_queue.py::test_a_logless_session_still_resets_the_no_progress_count -q
      - python3 -m pytest tests/test_session_queue.py::test_the_logless_breaker_is_recorded_as_a_breaker_not_a_failure -q
    frozen:
      - tests/test_session_queue.py
    scope:
      - edad/session_queue.py
    seam: queue-plan, queue-breaker, logs
    rejected: "re-validating the network before each ticket — the most precise message, but it only ever catches this one failure mode and puts a subprocess in the queue's hot loop"

  - id: D6
    decision: "`session_argv` always emits `--sandbox <tier>`, in every branch, so the child's recorded command states the tier rather than inheriting it and two defaults in two files cannot drift. `tests/test_session_queue.py:296` is amended before approval."
    verify:
      - python3 -m pytest tests/test_session_queue.py::test_session_argv_states_the_tier_even_when_it_is_the_default -q
      - python3 -m pytest tests/test_session_queue.py::test_each_ticket_runs_in_its_own_subprocess -q
    frozen:
      - tests/test_session_queue.py
    scope:
      - edad/session_queue.py
    seam: queue-builders
    rejected: "omitting --sandbox when it is the default — smaller diff and the frozen test untouched, but the tier would be asserted by two files' defaults agreeing and a running night's ps output would not say which tier it is"

  - id: D7
    decision: "The docker tier is proved by a container property test, opt-in behind `EDAD_DOCKER_TESTS=1` rather than an auto-skip, needing no API key: `--network none` has no egress; the named internal net reaches the fixture by name and still has no egress; a bridge-first fixture is reachable from the host."
    verify:                                     # hand-run, not a ticket — see D12
      - EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py::test_a_container_on_network_none_has_no_egress -q
      - EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py::test_a_container_on_the_internal_net_reaches_the_fixture_by_name_and_has_no_egress -q
    frozen:
      - docker_tests/test_properties.py
    seam: docker-property
    rejected: "an auto-skip in the full gate — a test that never bites, which this repo has a whole mutation-proof spec about"

  - id: D8
    decision: "`EDAD_NETWORK` is documented as a hint: the enforcement claim is scoped to the agent container, the verifier half is an advisory marker a cooperating suite may honour, and each run records which half was actually enforced."
    verify:
      - python3 -m pytest tests/test_session_network.py::test_the_session_log_records_which_half_of_the_deny_was_enforced -q
    frozen:
      - tests/test_session_network.py
    scope:
      - edad/session.py
    seam: logs
    rejected: "claiming enforcement on both sides — the verifier half is a string a suite may honour; real verifier-side enforcement is a named future decision, not an assumed-present one"

  - id: D9
    decision: "Egress reaches the model through a dual-homed proxy attached bridge-first then to the internal net; the agent stays on the internal net with zero egress of its own and reaches the model only through the proxy, and `network_rule`'s named-tier text becomes 'no internet egress except the model API, via the proxy'."
    verify:
      - python3 -m pytest tests/test_session_network.py::test_the_agent_container_is_pointed_at_the_proxy -q
      - python3 -m pytest tests/test_session_network.py::test_the_named_tier_rule_names_the_proxy_as_the_only_egress -q
    frozen:
      - tests/test_session_network.py
    scope:
      - edad/session.py
    seam: session-builders
    rejected: "accepting bridge egress and dropping the isolation claim — reduces --sandbox docker to a filesystem sandbox and hands the overnight agent the open internet; and marking the tier non-functional and shipping only plumbing, which leaves the queue permanently unsandboxed"

  - id: D10
    decision: "`--sandbox docker` without `--network` is refused at preflight with a message saying that branch can never run a real agent — one working docker configuration instead of one working and one broken. Supersedes T002's 'without --network the container runs --network none'; `test_the_default_tier_asks_docker_nothing` is amended before approval."
    verify:
      - python3 -m pytest tests/test_session_network.py::test_docker_without_a_network_is_refused_at_preflight -q
      - python3 -m pytest tests/test_session_network.py::test_the_default_tier_asks_docker_nothing -q
    frozen:
      - tests/test_session_network.py
    scope:
      - edad/session.py
    seam: session-preflight
    rejected: "keeping bare --sandbox docker for stub agents via EDAD_AGENT_CMD — the suite monkeypatches rather than using it, and it leaves the obvious invocation as the broken one"

  - id: D11
    decision: "`validate_network` probes actual egress: keep the `Internal: true` check and add a throwaway container on the network that tries to reach the internet unconfigured; refuse if it succeeds, and refuse if the probe could not be run. Once a forwarding proxy sits on the net, `Internal: true` no longer backs the prompt's claim on its own."
    verify:
      - python3 -m pytest tests/test_session_network.py::test_a_network_with_measured_egress_is_refused -q
      - python3 -m pytest tests/test_session_network.py::test_an_egress_probe_that_could_not_run_is_refused -q
      - python3 -m pytest tests/test_session_network.py::test_an_internal_network_is_accepted -q
    frozen:
      - tests/test_session_network.py
    scope:
      - edad/session.py
    seam: session-preflight
    rejected: "enumerating and rejecting dual-homed containers — self-defeating; the proxy is dual-homed by design, and dual-homing alone does not leak (a dual-homed fixture left the agent with no egress); a process that forwards does"

  - id: D12
    decision: "The cut: T013 the queue carries the tier (D1, D4, D6, D5's plan-time half, D17's queue half); T014 the logless breaker (D5's breaker half), separately because it changes when a night stops and deserves its own red proof; T015 the proxy daemon, image and lifecycle (D13, D14, D15's allowlist half, D17's lifecycle half); T016 preflight against the proxy (D9, D10, D11, D16, D18, D8's recording half, D15's container-env half, D17's session half). The docker property test (D2, D7) is written by hand. Order: T015 → T016 → T013; T014 independent."
    unenforced: "Structural. The ordering was the one item the grill left open; settled here by the operator, and confirmed by the seam pass as a dependency — T013's plan-time validation imports T016's validate_network and T015's lifecycle."
    seam: none
    rejected: "one ticket for all the code, or one ticket per change — bundling puts independent red proofs in one gate, where a failure in either makes the other's evidence ambiguous; see D12 in the grill"

  - id: D13
    decision: "The harness owns the proxy; the operator still owns fixtures. The proxy is one known image built from the checked-in daemon with one known allowlist, and it is the security boundary — an operator-supplied one that is misconfigured silently widens it, and the egress probe cannot see a permissive proxy."
    verify:
      - python3 -m pytest tests/test_egress_proxy.py::test_the_proxy_image_is_built_from_the_checked_in_daemon -q
      - python3 -m pytest tests/test_egress_proxy.py::test_ensure_starts_the_proxy_on_bridge_first_then_attaches_the_internal_net -q
    frozen:
      - tests/test_egress_proxy.py
    scope:
      - edad/egress.py
      - Dockerfile.egress
    seam: proxy-lifecycle
    rejected: "operator-supplied proxy — the boundary becomes something the harness cannot vouch for"

  - id: D14
    decision: "A CONNECT-target allowlist in a purpose-built minimal proxy: the target host arrives in plaintext in the `CONNECT` line before TLS, so the proxy matches it against the allowlist and splices or refuses — no interception, no certificate injection, no decryption; small enough to read in one screen, and exercised directly by D7's property test."
    verify:
      - python3 -m pytest tests/test_egress_proxy.py::test_connect_to_the_allowlisted_host_is_spliced_to_the_upstream -q
      - python3 -m pytest tests/test_egress_proxy.py::test_connect_to_any_other_host_is_refused_before_any_upstream_connection -q
      - python3 -m pytest tests/test_egress_proxy.py::test_a_raw_ip_target_is_refused -q
      - python3 -m pytest tests/test_egress_proxy.py::test_a_non_connect_request_is_refused -q
      - EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py::test_the_real_proxy_image_permits_only_the_model_api -q   # hand-run
    frozen:
      - tests/test_egress_proxy.py
      - docker_tests/test_properties.py
    scope:
      - edad/egress_proxy.py
    seam: proxy-daemon, docker-property
    rejected: "tinyproxy/squid — same mechanism but the boundary becomes a config with filter semantics easy to get wrong in the permissive direction; and a DNS allowlist, rejected outright because DNS does not bind the connection and a raw IP walks past it"

  - id: D15
    decision: "Telemetry off and the allowlist is one entry: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is set in the container, measured to drop the datadog host entirely, so the egress claim is 'the model API, nothing else'. Stated assumption: the container holds a long-lived `setup-token` and performs no OAuth refresh or authorize flow, so `platform.claude.com` is deliberately not allowlisted; widening the allowlist is not the fix for an expired token, D16 is."
    verify:
      # T015 — the allowlist half
      - python3 -m pytest tests/test_egress_proxy.py::test_the_allowlist_is_exactly_the_model_api -q
      # T016 — the container-env half
      - python3 -m pytest tests/test_session_network.py::test_nonessential_traffic_is_disabled_in_the_container -q
    frozen:
      - tests/test_egress_proxy.py
      - tests/test_session_network.py
    scope:
      - edad/egress_proxy.py
      - edad/session.py
    seam: proxy-daemon, session-builders
    rejected: "a two-entry allowlist keeping telemetry on — the allowlist is one entry, not two, if telemetry is disabled; and widening it for OAuth refresh, which a headless container cannot complete whatever hosts it can reach"

  - id: D16
    decision: "Preflight probes the proxy in both directions and the permit half is authenticated: a non-allowlisted host must be refused and `api.anthropic.com` must be permitted, the latter using the actual `CLAUDE_CODE_OAUTH_TOKEN` the container will receive, from the agent image, on the named network — so one call proves the proxy permits the API, the token is valid, and it is the token the agent will hold. Both directions, because a dead or block-everything proxy passes a refusal-only check and surfaces as the 15-retry storm at `AGENT_TIMEOUT_S`."
    verify:
      - python3 -m pytest tests/test_session_network.py::test_a_proxy_that_permits_a_non_allowlisted_host_is_refused -q
      - python3 -m pytest tests/test_session_network.py::test_a_proxy_that_refuses_the_model_api_is_refused -q
      - python3 -m pytest tests/test_session_network.py::test_the_permit_probe_uses_the_token_and_image_the_agent_will_hold -q
    frozen:
      - tests/test_session_network.py
    scope:
      - edad/session.py
    seam: session-preflight
    rejected: "an inspection-only check of the proxy image — knowing what the image contains is not knowing what is running; a stale image, a name collision or a hand-started container all pass it by assumption"

  - id: D17
    decision: "Whoever created the proxy destroys it. Deterministic name per network, `edad-egress-<network>`; the entry point ensures one exists, creating it if absent, and tears it down only if it was the creator — the queue ensures one before the run and removes it after, each session finds it present and leaves it, a standalone session creates and removes its own. No new flag, and still correct when sessions become concurrent."
    verify:
      # T015 — the lifecycle half
      - python3 -m pytest tests/test_egress_proxy.py::test_the_proxy_name_is_deterministic_per_network -q
      - python3 -m pytest tests/test_egress_proxy.py::test_ensure_creates_only_when_absent_and_reports_whether_it_did -q
      # T016 — the session half
      - python3 -m pytest tests/test_session_network.py::test_a_standalone_session_creates_and_removes_its_own_proxy -q
      - python3 -m pytest tests/test_session_network.py::test_a_session_leaves_a_proxy_it_found_already_present -q
      # T013 — the queue half
      - python3 -m pytest tests/test_session_queue.py::test_the_queue_ensures_the_proxy_before_the_run_and_removes_it_after -q
      - python3 -m pytest tests/test_session_queue.py::test_the_queue_leaves_a_proxy_it_did_not_create -q
    frozen:
      - tests/test_egress_proxy.py
      - tests/test_session_network.py
      - tests/test_session_queue.py
    scope:
      - edad/egress.py
      - edad/session.py
      - edad/session_queue.py
    seam: proxy-lifecycle, session-builders, queue-plan
    rejected: "per-session create/destroy — N container starts a night, and it breaks under the scheduler the controller spec was left cheap to add"

  - id: D18
    decision: "The token crosses as an env pass-through — `agent_argv` keeps the bare `-e CLAUDE_CODE_OAUTH_TOKEN` form and `run_agent` inherits the environment, so the value never appears in argv — and preflight refuses `--sandbox docker` when the variable is unset, naming `claude setup-token`, because `docker run -e VAR` with VAR unset passes nothing and raises no error. D16 covers set-but-invalid; this exists because 'unset' deserves a better message than a failed API call."
    verify:
      # T016 — both halves are session.py; listed by seam
      - python3 -m pytest tests/test_session_network.py::test_docker_without_an_oauth_token_is_refused_naming_setup_token -q
      - python3 -m pytest tests/test_session_network.py::test_the_token_value_never_appears_in_argv -q
    frozen:
      - tests/test_session_network.py
    scope:
      - edad/session.py
    seam: session-preflight, session-builders
    rejected: "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR — a real first-class auth source, but docker run does not forward file descriptors into a container; the residual exposure of the env form (docker inspect, /proc inside the container) is accepted because the agent needs the credential to function"

  - id: D19
    decision: "The agent can run its own checks, one rule per tier. Under `--sandbox docker` the container is the permission boundary and the CLI runs with `--dangerously-skip-permissions` unconditionally; the agent image runs as a non-root user because the CLI refuses that flag as root. On the host the agent keeps `--permission-mode acceptEdits` and gets `--allowedTools` naming the ticket's own gate commands — each `acceptance` and `full_gate` command exactly as `Bash(<command>)`, plus `Bash(<tokens through pytest>:*)` for each command that invokes pytest — with the prompt placed ahead of the variadic flag; `--yolo` stays the host-tier opt-in for bypass. The session log records `permissions: {mode, allowed}` beside `network_access`. Settled 2026-09-11 from the session logs of the tier's first night: every ticket to date was implemented by an agent that could not run a command."
    verify:
      - python3 -m pytest tests/test_session_permissions.py::test_docker_bypasses_permissions_whatever_yolo_says -q
      - python3 -m pytest tests/test_session_permissions.py::test_the_host_tier_accepts_edits_and_allows_the_gate_commands_with_the_prompt_first -q
      - python3 -m pytest tests/test_session_permissions.py::test_yolo_on_the_host_bypasses -q
      - python3 -m pytest tests/test_session_permissions.py::test_allowed_tools_names_each_gate_command_once_and_a_pytest_prefix -q
      - python3 -m pytest tests/test_session_permissions.py::test_permissions_says_what_the_agent_could_do_per_tier -q
      - python3 -m pytest tests/test_session_permissions.py::test_the_session_log_records_permissions_beside_network_access -q
      - python3 -m pytest tests/test_session_permissions.py::test_the_agent_image_does_not_run_as_root -q
      - EDAD_DOCKER_TESTS=1 python3 -m pytest docker_tests/test_properties.py::test_the_agent_image_accepts_the_bypass_flag_as_its_user -q   # hand-run, after the image is rebuilt
    frozen:
      - tests/test_session_permissions.py
      - docker_tests/test_properties.py
    scope:
      - edad/session.py
      - Dockerfile.agent
    seam: session-builders, docker-property
    rejected: "`--yolo` passed through the queue — the operator opts in per night and every night without the flag silently repeats the blind agent; an allowlist inside the container — forbids ls, grep and a scratch script in a container that is already the boundary; `IS_SANDBOX=1` to run bypass as root — an undocumented escape hatch where a `USER` line is the honest fix"

deferred:
  - "N for the logless breaker (D5): spec-stage default MAX_NO_PROGRESS; confirm before T014 is approved"
  - "what ensure_egress_proxy does when the image is absent: spec-stage default is refuse naming the build command; re-grill if the harness should build it"
  - "--image pass-through on the queue (--yolo and acceptEdits settled as D19)"
  - "re-measuring the allowlist against a new CLI release; D7's property test is the natural home and does not do it"
  - "verifier-side network enforcement; D8 records, does not enforce"
  - "--dry-run under --sandbox docker: spec-stage default is isolation check only, probes skipped and said so; confirm before T016"
  - "two standalone sessions on one network race on ensure; the queue is single-owner and safe, standalone-concurrent is not — a sentence in T015's body, not a design"
```
