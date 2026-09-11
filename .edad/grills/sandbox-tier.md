# Grill record — the sandbox tier

Grilled 2026-09-10. Branch `main` @ `8b033fb`.
Origin: a grill on **T002**, which shipped clean on 2026-09-08 (`.edad/evidence/T002.json`:
full gate passed, only `edad/session.py` touched) and has `spec: null` — it was decided in
conversation and never written down. This record supersedes T002's `## Out of scope`
section, which contains a claim that is false when measured. It does not amend `T002.md`
itself; see D3.

## The problem

T002 built a network tier for the docker sandbox and wired it end to end in the
single-ticket path (`edad/session.py:604` preflight, `:616` prompt, `:631` argv). Two
things were then true and neither was written down:

1. **The controller cannot reach it.** `session_argv()`
   (`edad/session_queue.py:101-113`) returns `[python, -m, edad.session, run, <id>]` with
   no `--sandbox` and no `--network`. `--sandbox` defaults to `none`
   (`edad/session.py:710`). So the unattended overnight run — the one path where nobody
   is watching the agent — is the one path that never sandboxes. The
   multi-ticket-controller spec never uses the words "sandbox" or "network". This was
   not decided against; it was never raised.

2. **The tier has never run.** `edad-agent:latest` is not built on this machine. All 22
   session logs under `.edad/sessions/` record `sandbox: none`. Every T002 test is a
   monkeypatched probe or a pure argv builder, so the tier is verified as a
   string-builder and not as a sandbox.

The second fact turned out to matter more than the first.

## What was measured

Run against live docker during the grill, torn down after. These are the primary
observations the decisions below rest on; nothing here is recalled or reasoned-to.

| probe | result |
|---|---|
| agent reaches a service by container name on an internal net | **YES** |
| agent egress to the internet from an internal net | **none** |
| service on an internal net publishes a host port (`-p`) | **fails**, connection refused |
| …after also `docker network connect bridge <svc>` | **still fails** — mappings are fixed at `run` time |
| service started on bridge with `-p`, *then* attached to the internal net | host **200**, agent by-name **YES**, agent egress **still none** |
| container on `--network none` → `api.anthropic.com` | **bad address** — no DNS, no egress |
| same container on default bridge → `api.anthropic.com` | reaches the server (404 from the API root) |

Two consequences, in ascending order of severity:

- **T002's fixture claim is false.** Its `## Out of scope` says "a test database serving
  the agent on an internal network is expected to publish a host port for the verifier to
  reach." A container attached to an internal network cannot publish a host port at all.
  The only ordering that works is dual-homing bridge-first, then attaching the internal
  net.
- **The docker tier cannot run an agent.** `agent_argv` passes
  `-e CLAUDE_CODE_OAUTH_TOKEN` into the container, so the design intends API access — but
  both docker branches emit `--network none` or a network measured to have zero egress.
  `claude -p` inside that container cannot reach the model. T002's out-of-scope line
  "egress filtering finer than docker's internal flag" is not a nicety deferred; it is the
  thing the whole tier is blocked on.

A third thing, found by reading rather than probing: `network_access: deny` is enforced
on the agent side (a real `--network none`) and merely *hinted* on the verifier side —
`run_commands()` implements it as `env["EDAD_NETWORK"] = "deny"`
(`edad/gate.py:428-438`), a string a cooperating suite may honour. `Dockerfile.agent`'s
own comment already says this ("`--network none` at run time is what makes the ticket's
`network_access: deny` real rather than advisory"); the ticket schema does not.

## What was measured — second pass, the egress design

Run against the installed `claude` 2.1.263 with `HTTPS_PROXY`/`HTTP_PROXY` pointed at a
throwaway listener that logged the first line of every connection and closed it.

| probe | result |
|---|---|
| does `claude` honour `HTTPS_PROXY` / `HTTP_PROXY` | **yes** |
| what it sends | `CONNECT api.anthropic.com:443 HTTP/1.1` — target host in **plaintext, pre-TLS** |
| hosts dialled, default config | `api.anthropic.com:443` and `http-intake.logs.us5.datadoghq.com:443` |
| hosts dialled with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | **`api.anthropic.com:443` only** (8 of 8 connections) |
| behaviour against a proxy that refuses | **15 retries in ~20s**, no fast failure |

Three consequences. A hostname allowlist needs **no TLS interception** — the host arrives
before the handshake, so the proxy splices or refuses without decrypting anything. The
allowlist is **one entry**, not two, if telemetry is disabled. And a too-narrow allowlist
does not fail cleanly: it produces a retry storm that ends at `AGENT_TIMEOUT_S`, which is
the failure mode the verification decision below exists to catch at plan time.

**These were measured on the OAuth path, which is the path the tier uses.** `agent_argv`
passes `-e CLAUDE_CODE_OAUTH_TOKEN`, and the machine the probes ran on authenticates with
OAuth (`~/.claude.json` carries `oauthAccount`; no `ANTHROPIC_API_KEY` in the environment).
So the one-host result is an OAuth result, not an API-key result.

It is nonetheless a result for a run holding a **valid** token. `strings` on the CLI binary
shows an OAuth token endpoint on a *different* host — `https://platform.claude.com/v1/oauth/token`,
alongside `platform.claude.com/oauth/authorize` and `claude.ai/oauth/claude-code-client-metadata`
— which a 20-second probe would never exercise. The binary's own help text resolves why
that does not widen the allowlist: *"`setup-token` creates a long-lived Claude.ai
subscription token"*, supplied via `CLAUDE_CODE_OAUTH_TOKEN`. The container is handed a
long-lived token and performs no refresh or authorize flow, so it never dials those hosts.
See the assumption recorded in D15.

## What was measured — third pass, the full path end to end

Run 2026-09-11 at spec stage, after the seam pass raised two things the first two passes
had not measured: whether the CLI resolves the API hostname itself before using the proxy
(it cannot, on an internal net), and what the D16 permit probe should actually call. Rig:
`edad-agent:latest` built from `Dockerfile.agent` (22s; it did not exist until now), an
internal net, and a ~60-line stdlib CONNECT proxy dual-homed bridge-first per D9 — run in
refuse mode, then forward mode with a one-entry allowlist. Torn down after; the image is
kept.

| probe | result |
|---|---|
| unconfigured container on the internal net: DNS `api.anthropic.com` | fails in **0.00s**, name resolution |
| unconfigured container on the internal net: raw TCP `1.1.1.1:443` | fails in **0.00s**, `Network is unreachable`; on bridge, reached in 0.06s |
| `claude -p` from that DNS-less container with `HTTPS_PROXY` set | sends **`CONNECT api.anthropic.com:443`** — hostname in the CONNECT line, no local DNS |
| …against a proxy that refuses everything (403) | **13 retries in 20s, exit 1**, `Failed to authenticate. API Error: 403 status code (no body)` |
| …through the forwarding proxy, deliberately invalid token | **3s, exit 1**, `Failed to authenticate. API Error: 401 OAuth access token is invalid` |
| …through the forwarding proxy, valid `setup-token`, `--max-turns 1` | **4s, exit 0**, reply `ok` |
| hosts dialled in every run above, telemetry off | `api.anthropic.com:443` only |
| configured container, `CONNECT example.com` / `CONNECT 1.1.1.1` | **403 in 0.01s**, no upstream attempt; `api.anthropic.com` spliced |

Four consequences, each of which lands in the spec rather than reopening a decision.

- **The CLI hands the proxy a hostname.** DNS inside the container is irrelevant to the
  tier; the worry that the CLI might pre-resolve and fail before reaching the proxy is
  retired by measurement.
- **D11's egress probe is a raw-IP TCP connect.** DNS-free, and instant in both
  directions — it adds nothing to plan time and needs no timeout to wait on.
- **D16's permit probe is `claude -p` itself**, `--max-turns 1`, in the agent image, on
  the named network, with the real token. It is the client the agent will use, and a bad
  token fails in 3s — the expensive case is the working one, and it costs one trivial
  model turn. There is no cheaper authenticated call that does not hardcode the CLI's
  own headers.
- **D16's two directions are distinguishable by status and shape, not by message.** A
  dead proxy and a bad token both print `Failed to authenticate`; they differ as 403
  with 20s of retries versus 401 in 3s. A refusal-only check passes a dead proxy, and the
  night then surfaces it as that 20s storm repeated to `AGENT_TIMEOUT_S`.

## Decisions

Numbered within this spec.

**D1 — The queue carries the tier.** `session_queue run` gains `--sandbox` and
`--network`, threaded through `session_argv()` to every child session. One decision per
night, uniform across the queue. This keeps T002's "the operator supplies an existing
network" model and does not reopen per-ticket declaration.

**D2 — The fixture contract is dual-homed, and the harness still creates nothing.**
Operator supplies both the network and the fixture. The supported recipe is: start the
fixture on bridge with `-p`, *then* `docker network connect <internal> <fixture>`. The
verifier stays on the host and reaches it at `127.0.0.1:PORT`; `run_commands()` is
untouched and T002's out-of-scope boundary holds.

**D3 — Corrections land in a new spec, not in `T002.md`.** Cut
`.edad/specs/sandbox-tier.md` carrying these decisions, retro-covering T002's tier. The
new ticket cites it via `spec:` + `decisions:`. Editing `T002.md` in place was rejected
because `ticket_sha256` is checked at every session start (`edad/gate.py:293-306`): the
edit would make T002 refuse to run as "modified since approval", and would rewrite the
record of what was approved at the moment its evidence was earned.

**D4 — The queue defaults to `--sandbox none`.** Parity with `session.py`; nothing about
tonight changes silently. The run log gains the tier so the morning can answer "did this
night run sandboxed". Defaulting to `docker` was rejected: it would promote a default to
a code path that has never executed, which is the failure mode this harness exists to
prevent.

**D5 — Validate up front, and add a logless-session breaker.** `validate_network()` runs
once at plan time, before anything executes, matching D1 of the controller spec (the plan
is computed before execution) — a missing network refuses the run at ticket 0. Plus a
third breaker: N consecutive sessions that produced *no session log at all* stop the
night as an environment failure.

  The hole this closes: `preflight()` runs at `edad/session.py:604`, **before**
  `SessionLog` is constructed at `:607`. A `validate_network` abort therefore exits 2
  writing no log, the queue hits `fail(id, session_log=None)`
  (`edad/session_queue.py:643-657`), and that path *resets* `no_commit_aborts` rather
  than raising it. Tear the fixture network down at ticket 3 of 12 and tickets 4–12 each
  abort instantly, each reset the breaker, the queue drains, and the run log says it
  stopped because it ran out of tickets. The existing reset stays correct for its own
  case — a logless session is distinguishable evidence, not absent evidence.

**D6 — `session_argv` always emits the tier.** `--sandbox <tier>` is emitted in every
branch, matching T002's own "exactly one `--network` flag either way" rule. The child's
recorded command states the tier rather than inheriting it, and two defaults in two files
cannot drift apart. This requires amending `tests/test_session_queue.py:296`, which pins
`session_argv("T004")` by exact list equality — amended **before** approval, which is the
documented procedure for a frozen stand-in that pins the wrong arity, not a violation of
it. `breaker_fired()` needs no amendment: its call sites (`:736-738`) are keyword-only,
so a fourth defaulted keyword is free.

**D7 — The docker tier is proved by a container property test, opt-in.** Today's hand
probes become a repeatable test needing no API key: a container on `--network none` has
no egress; a container on the named internal net reaches the fixture by name and still
has no egress; a fixture published bridge-first is reachable from the host. It tests the
load-bearing *claim* rather than the agent. Gated behind `EDAD_DOCKER_TESTS=1` rather
than an auto-skip — an auto-skip in the full gate is a test that never bites, which this
repo has a whole `mutation-proof` spec about.

**D8 — `EDAD_NETWORK` is documented as a hint.** The enforcement claim is scoped
explicitly to the agent container; the verifier half is an advisory marker a cooperating
suite may honour, and says so. Each run records which half was actually enforced. Real
verifier-side enforcement stays a named future decision rather than an assumed-present
one.

**D9 — Egress reaches the model through a dual-homed proxy.** A proxy container attached
bridge-first and then to the internal net — the same pattern D2 uses for fixtures. The
agent stays on the internal net with zero egress of its own and reaches the model only
through the proxy, which forwards to `api.anthropic.com` and nothing else.
`network_rule`'s named-tier text becomes "no internet egress except the model API, via
`<proxy>`", which is measurable. The frozen assertions for that branch
(`tests/test_session_network.py:156-164`) are `"internet" in text` and `"installs" in
text`, loose enough that this prose correction needs **no** amendment.

**D10 — `--sandbox docker` without `--network` is refused at preflight.** That branch can
never run a real agent, so it is refused with a message saying so, rather than failing
later with a confusing DNS or auth error. One working docker configuration instead of one
working and one broken. This supersedes T002's "Without `--network`, nothing changes: the
container runs `--network none`", and costs an amendment to
`tests/test_session_network.py`'s plain-docker tests, pre-approval.

**D11 — `validate_network` probes actual egress.** Keep the `Internal: true` check and
add a throwaway container on the network that tries to reach the internet unconfigured;
refuse if it succeeds. Once a forwarding proxy sits on the net, `Internal: true` no longer
backs the claim the prompt makes — the probe must measure "a container placed here has no
egress of its own" rather than a flag that used to correlate with it. Measured cost is one
container start, and per D5 it happens once at plan time. Note the nuance the probes
establish: dual-homing alone does not leak (probe D — a dual-homed fixture left the agent
with no egress); a process that *forwards* does.

**D13 — The harness owns the proxy; the operator still owns fixtures.** The line is
verifiability: the harness creates what it must be able to vouch for, the operator creates
what only they know. The proxy is one known image with one known allowlist and it *is* the
security boundary — an operator-supplied one that is misconfigured silently widens that
boundary, and D11's egress probe cannot see a permissive proxy. Fixtures stay the
operator's, because they are arbitrary and ticket-specific. This narrows D2 rather than
overturning it, and the session gains a container lifecycle it did not have.

**D14 — A CONNECT-target allowlist in a purpose-built minimal proxy.** Measured: the
target hostname arrives in plaintext in the `CONNECT` line, before TLS. So the proxy
matches the host against a one-entry allowlist and either splices the connection or
refuses it — no interception, no cert injection, no decryption. It is deliberately small
enough to read in one screen, because the allowlist is the entire boundary, and it is
exercised directly by D7's property test. `tinyproxy`/`squid` were rejected: same
mechanism, but the boundary becomes a config with filter semantics that are easy to get
wrong in the permissive direction. A **DNS allowlist** was rejected outright — DNS does
not bind the connection, so anything dialling a raw IP walks past it, which would make the
prompt's egress claim unenforced: exactly what T002 exists to prevent.

**D15 — Telemetry off; the allowlist is one entry.**
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is set in the container, measured to drop
`http-intake.logs.us5.datadoghq.com` entirely. The agent's egress claim becomes "the model
API, nothing else", which is both true and short, and a one-entry allowlist is auditable at
a glance. Cost, accepted: no telemetry from sandboxed runs.

  **Stated assumption, because the allowlist is only correct under it:** the container is
  given a *long-lived* token minted by `claude setup-token` and performs no OAuth refresh
  or authorize flow. Those go to `platform.claude.com`, which is deliberately not on the
  allowlist. If anyone instead mounts the host's stored credentials — which do refresh —
  this allowlist is wrong and the run dies as the measured retry storm. Widening the
  allowlist is *not* the fix: a headless container cannot complete a refresh or an
  interactive re-auth whatever hosts it can reach. The fix for an expired token is D16.

**D16 — Preflight probes the proxy in both directions, and the permit half is
authenticated.** Assert that a non-allowlisted host is **refused** and that
`api.anthropic.com` is **permitted**, alongside D11's unconfigured-egress check. The
permit probe uses the actual `CLAUDE_CODE_OAUTH_TOKEN` the container will receive, so one
call proves three things: the proxy permits the API, the token is valid, and it is the
token the agent will hold. This is the only check that catches an expired token, which
D15 establishes cannot be recovered inside a headless container by any allowlist. Cost,
accepted: preflight makes one real authenticated call, so it touches the account's usage —
trivially small, but non-zero. Both directions, because a dead or block-everything proxy
passes a refusal-only check and then surfaces as the measured 15-retry storm ending at
`AGENT_TIMEOUT_S` in the middle of the night. Knowing what the image contains is not
knowing what is running — a stale image, a name collision, or a hand-started container all
pass an inspection-only check by assumption.

**D17 — Whoever created the proxy destroys it.** Deterministic name per network
(`edad-egress-<network>`). The entry point ensures one exists, creating it if absent, and
tears it down only if it was the creator: the queue ensures one before the run and removes
it after, each session finds it already present and leaves it, a standalone session creates
and removes its own. One rule covers both entry points with no new flag, and it stays
correct when D13 of the controller spec makes sessions concurrent. Per-session
create/destroy was rejected as N container starts a night that breaks under the scheduler
that spec was left cheap to add.

**D18 — The token crosses as an env pass-through, and preflight refuses when it is unset.**
`agent_argv` already emits the bare `-e CLAUDE_CODE_OAUTH_TOKEN` form and `run_agent`
inherits `os.environ`, which is the right shape: the **value never appears in argv**, so it
cannot leak into `ps` or any logged command line. Measured: `docker run -e VAR` with `VAR`
unset in the parent passes *nothing* and raises no error, so today an operator who has not
minted a token gets a container with no credential and no warning — a third silent failure
of the tier, alongside the missing image and the missing egress. Preflight therefore
refuses `--sandbox docker` when `CLAUDE_CODE_OAUTH_TOKEN` is unset, naming
`claude setup-token` in the message. D16's authenticated probe then covers the harder case
(set but expired or invalid); this check exists because "unset" deserves a better message
than a failed API call.

  Rejected: `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`. It is a real first-class auth source
  in the CLI and would keep the token out of the container's environment, but `docker run`
  does not forward file descriptors into a container — that path is for locally-spawned
  subprocesses. The residual exposure of the env form (`docker inspect`, `/proc` inside the
  container) is accepted: the agent needs the credential to function, so hiding it from the
  agent is not a goal.

## Rejected

- **Per-ticket network declaration in frontmatter** (against D1). Right only if tickets
  genuinely need different services; it makes the single-ticket path honour the field
  too, and puts a security-relevant field inside the approval hash.
- **Harness manages the fixture lifecycle** (against D2). Would remove a real operator
  footgun — the wrong start order silently produces an unreachable service — but T002
  excluded creation/teardown, and it drags compose-shaped lifecycle into a harness that
  shells out to docker exactly once.
- **Joining the verifier to the network** (against D2). Fixture would need no dual-homing
  and the verifier would get the agent's isolation, but it contradicts
  `run_commands()`-on-host across `gate.py`'s seven call sites and reopens the verifier
  design T002 deliberately walled off.
- **Editing `T002.md` in place** (against D3) — breaks its approval lock; see D3.
- **Requiring an explicit `--sandbox` on the queue** (against D4). Makes the risk
  un-inheritable, but breaks every existing invocation and gets aliased away in a week.
- **Defaulting the queue to `docker`** (against D4) — broken on arrival, no image exists.
- **Re-validating the network before each ticket** (against D5). Most precise message and
  catches teardown at the next ticket, but only ever catches this one failure mode and
  puts a subprocess in the queue's hot loop.
- **Omitting `--sandbox` when it is the default** (against D6). Smaller diff, frozen test
  untouched — but the tier would then be asserted by two files' defaults agreeing, and a
  running night's `ps` output would not say which tier it is.
- **One ticket for all the code / four tickets, one per change** (against D12). See D12.
- **Accepting bridge egress and dropping the isolation claim** (against D9). Simplest
  thing that runs, but reduces `--sandbox docker` to a filesystem sandbox and hands the
  unattended overnight agent the open internet — the thing this grill started from.
- **Marking the tier non-functional and shipping only plumbing** (against D9). Most
  honest about today and costs nothing to be wrong about, but leaves the queue
  permanently unsandboxed: the status quo with better documentation.
- **Keeping bare `--sandbox docker` for stub agents** (against D10). `EDAD_AGENT_CMD`
  exists precisely to override the agent command and a stub needs no egress — but the
  suite monkeypatches rather than using it, and it leaves the obvious invocation as the
  broken one.
- **Enumerating and rejecting dual-homed containers** (against D11) — self-defeating; the
  proxy is dual-homed by design.

## The cut

**D12 — Two tickets and one hand-written test.**

- **T013** — the queue carries the tier: D1, D4, D6, plus plan-time validation from D5 and
  the run-log tier field. Scope `edad/session_queue.py`; frozen
  `tests/test_session_queue.py`, amended pre-approval per D6.
- **T014** — the logless-session breaker (the rest of D5), separately, because it changes
  *when a night stops* and deserves its own red proof. Bundling it with T013 would put two
  independent red proofs in one gate, where a failure in either makes the other's evidence
  ambiguous.
- **The docker property test (D7) is written by hand.** It has no valid EDAD shape: its
  deliverable is a test file, which inverts the `frozen`-as-spec / `scope`-as-implementation
  roles. T011 is not a precedent — its scope is `ytmp3/converter.py` with "nothing to
  implement", the mutation tier applied to existing code. Forcing a shape here would mean
  freezing a test that tests the test.

The egress design (D9–D11, D13–D17) is the rest of the work, and it is what decides
whether `--sandbox docker` is usable at all:

- **T015** — the proxy: the CONNECT-allowlist daemon and its image (D14, D15), plus the
  ensure/teardown lifecycle (D13, D17). Its own ticket because it is the security
  boundary and should be reviewable on its own.
- **T016** — preflight against the proxy: `validate_network`'s egress probe and the
  both-directions proxy probe (D11, D16), the `--sandbox docker` refusal without
  `--network` (D10), and the `network_rule` prose correction (D9). Scope
  `edad/session.py`; frozen `tests/test_session_network.py`, amended pre-approval per
  D10.

T013 and T014 are plumbing and can land first, but nothing about a night changes until
T015 and T016 do. **Open: whether the cut runs T013→T016 in order, or leads with T015/T016
so the first thing that lands is the thing that makes a run sandboxed.** Owner: chandra.

## Not settled here
- **Verifier-side network enforcement.** D8 makes the current state honest; it does not
  make it enforced. Named, not scheduled.
- **Egress filtering finer than one allowlisted host.** Still out of scope, now for the
  first time deliberately.
- **`--image` and `--yolo` pass-through on the queue.** D1 threads `--sandbox` and
  `--network` only. `--yolo` is documented as "only meaningful with `--sandbox docker`",
  and the non-yolo path uses `--permission-mode acceptEdits`
  (`edad/session.py:384`), which auto-accepts edits but not commands — so an agent inside
  the sandbox may be unable to run its own checks. Not measured, because the tier has
  never run. Re-raise when T015 lands.
- **Token minting and rotation.** D15 assumes a long-lived `setup-token`; nothing says who
  mints it, where it is stored on the host, or what happens when it expires. D16 turns
  expiry into a plan-time refusal rather than a wasted night, which is detection, not a
  rotation story.
- **What happens when the CLI's required hosts change.** D15 pins the allowlist to one
  measured entry. A future `claude` release needing a second host fails as the measured
  retry storm — caught by D16 at plan time, but only if the probe is re-run against the new
  CLI. Nothing re-measures the allowlist automatically; D7's property test is the natural
  home for that and does not currently do it.
