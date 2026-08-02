# Genblaze SDK — developer-experience feedback

**Status: internal draft. Not published anywhere yet.**

Prepared for the Backblaze Generative Media Hackathon (Feedback Prize).
Author: Miguel García (GitHub `migarci2`).
Date: 2026-08-02.

---

## 1. How this was produced

Everything below was reproduced by hand, not inferred from reading code.

| | |
|---|---|
| Install under test | `pip install genblaze` into an empty venv, Python 3.13, Linux |
| Resolved versions | `genblaze` 0.4.5, `genblaze-core` 0.3.8, `genblaze-s3` 0.3.6, connectors 0.3.2–0.3.5 |
| Source under test | `backblaze-labs/genblaze` @ `af84f8b` (v0.7.0 wave), full workspace via `make install-dev` |
| Test baseline | `make test` green on `af84f8b` before any change |

Three categories are kept separate on purpose:

- **Shipped as pull requests** — a mechanical fix exists, it is testable, and it does
  not require a maintainer product decision. (#258, #259, #260)
- **Filed as an issue** — reproducible, but the fix is a policy call that belongs to
  the Genblaze team, so a drive-by PR would be presumptuous. (#261)
- **Feedback only** — either already tracked upstream, or not independently
  reproducible here without credentials we do not have.

A note on overlap: after writing the first draft we found
`docs/exec-plans/feedback.md` in the repo and discovered the team already tracks
most of these (rows P1-01, P1-02, P1-12, P3-15…). We deliberately did **not**
delete the duplicates. Independent rediscovery is the useful signal: it tells you
which rows a brand-new user actually trips over in the first hour, versus which
ones only show up in an audit. Where a row already exists, it is cited.

---

## 2. Summary, ordered by impact

| # | Problem | Severity | Who hits it | Shape of fix | Status |
|---|---|---|---|---|---|
| 1 | `preflight=True` is the default and it runs an inverted `validate_model()` — the SDK ships a check that lies | **P0** | Every user of every provider with `DiscoverySupport.PERMISSIVE` | Fix inversion, then decide whether preflight stays default-on | Feedback (issue #248 open) |
| 2 | The shipped example `examples/batch_with_templates.py` crashes on its first `PromptTemplate` line | **P0** | Anyone who runs the batch example | 8-line `__init__` shim | **PR #258** |
| 3 | `from genblaze_core.testing import MockProvider` → `ModuleNotFoundError: pytest` on a clean install; this is the `libs/core/README.md` zero-API-key quickstart | **P0** | Anyone evaluating the SDK without paying for API calls | Move 1 import into 4 methods | **PR #259** |
| 4 | B2 credentials use `B2_APP_KEY`, but Backblaze's own samples/CLI use `B2_APPLICATION_KEY`; `B2_ENDPOINT` is ignored entirely | **P1** | Every Backblaze-ecosystem user | Accept both names as aliases | Feedback (row P1-12) |
| 5 | The whole GMI Cloud audio modality is unreachable — the param allowlist drops the API's required field | **P1** | Every GMI Cloud TTS/music user | Add `text`/`lyrics` to allowlist | Feedback (issue #251) |
| 6 | `@dataclass` on a `SyncProvider` subclass silently breaks the provider; failure surfaces ~1300 lines away as `AttributeError: '_retry_policy_override'` | **P1** | Anyone writing a custom provider the idiomatic-Python way | `__init_subclass__` guard | **Issue #261** |
| 7 | Configuration is split across the constructor, fluent builders, and `run()` kwargs with no discoverable rule; `run(cache=...)` dies with a bare `TypeError` | **P2** | Everyone, repeatedly | Better error + one config table in docs | **PR #260** (error only; the alias is still a design call) |
| 8 | A successful fallback erases the failed primary attempt from provenance | **P2** | Anyone relying on manifests as an audit trail | Record all attempts | Feedback (issue #239) |
| 9 | Examples and docs drift from runtime signatures with nothing gating it | **P2** | Everyone | `make docs-check` in CI | Feedback (row P3-15) |
| 10 | PyPI lags `main` by a full wave, and the wave tag looks like a version | **P3** | Anyone pinning versions | Version-compat table | Feedback (issue #254, partly addressed by #255) |

---

## 3. Detail

### 1. `preflight=True` is default-on and the check it runs is inverted — **P0**

**Symptom.** `Pipeline.__init__` takes `preflight: bool = True`. With the default,
`run()` calls `BaseProvider.validate_model()` on every step before executing
anything. Issue #248 reports that `validate_model()` returns `ok_authoritative`
for model slugs that 404 on real submit, and `unknown_permissive` for slugs that
work.

**Verified.**

```python
>>> import inspect; from genblaze_core import Pipeline
>>> inspect.signature(Pipeline.__init__).parameters["preflight"]
<Parameter "preflight: 'bool' = True">
```

and in `genblaze_core/pipeline/pipeline.py`, the module's own comment:

```
# preflight (_check_step_capabilities + _validate_models) calls
# validate_model() on each step's provider.
```

**Why this is the most serious item in the list.** The other bugs fail loudly.
This one succeeds loudly. A user reads "preflight passed", believes the model
slug is good, and then debugs a 404 for twenty minutes assuming it must be
credentials, region, or quota — because the SDK already told them the model was
fine. Worse, it inverts in *both* directions: a working slug reported as
`unknown_permissive` trains users to ignore the warning channel entirely, which
is the channel you need them reading. A wrong safety check is strictly worse than
no safety check, because it consumes the user's trust budget and then spends it
against you.

It is also on by default, so the blast radius is 100% of users, not the subset
who opt in.

**Proposal.**

1. Fix the inversion (issue #248) — this is the bug.
2. Then treat "should preflight be default-on" as a separate decision. Our
   recommendation: keep it on, but make the outcome vocabulary express
   confidence rather than a verdict. `ok_authoritative` /
   `unknown_permissive` read as verdicts; something like
   `confirmed_present` / `could_not_confirm` / `confirmed_absent` cannot be
   misread as a promise.
3. Add one contract test per connector — "a slug known to 404 must not validate
   as authoritative" — to the existing `ProviderComplianceTests` harness. That
   harness is the right home; it already runs for 10+ connectors, so the guard
   is nearly free and catches the next connector that gets the direction wrong.

**Effort:** small for the inversion; the compliance-test guard is the durable part.

---

### 2. `PromptTemplate("literal")` crashes — the shipped example is broken — **P0**

**Symptom.**

```console
$ python -c "from genblaze_core import PromptTemplate; PromptTemplate('A {animal}')"
TypeError: BaseModel.__init__() takes 1 positional argument but 2 were given
```

`examples/batch_with_templates.py:19` uses exactly that form, so the example dies
on its first `PromptTemplate` line — before it reaches a provider, before it
needs an API key.

**Root cause.** `PromptTemplate` is a Pydantic v2 model and `BaseModel.__init__`
is keyword-only. Note that a `model_validator(mode="before")` — the fix shape
named in the repo's own plan — cannot work here: the `TypeError` is raised by
`__init__` before any validator runs. The fix has to be an `__init__` shim.

**Impact.** This is a first-contact failure with a confusing error: the traceback
names `BaseModel`, a class the user never wrote, so it reads as "the library is
broken" rather than "call it differently".

**Proposal.** Accept the template positionally or by keyword. Shipped as
**PR #258** with 9 tests and a docs fix.

**Systemic point.** The examples directory is not executed by CI. We smoke-ran
all 30 examples with dummy credentials; only the classes of failure that survive
without real keys are visible, and this one was caught that way in seconds. See
item 9.

---

### 3. `genblaze_core.testing` requires pytest at import — **P0**

**Symptom.** On a clean `pip install genblaze-core` (pytest is a dev dependency,
not a runtime one):

```console
$ python -c "from genblaze_core.testing import MockVideoProvider"
  File ".../genblaze_core/testing.py", line 41, in <module>
    import pytest
ModuleNotFoundError: No module named 'pytest'
```

That exact line is the **zero-API-key quickstart in `libs/core/README.md:76`** —
the first runnable snippet in the README that ships on PyPI.

**Root cause.** The 0.3.5 fix moved the mocks to the pytest-free
`genblaze_core.mocks` and left `testing.py` re-exporting them. The re-export
works; the module-level `import pytest` above it does not. So the module
docstring's promise — *"Callers importing from `genblaze_core.testing` continue
to work unchanged"* — is not kept, and the CHANGELOG says the same thing.

**Impact.** This breaks the specific path a careful evaluator takes: try the SDK
without spending money on API calls first. The people most likely to hit it are
the people deciding whether to adopt.

**Proposal.** Import pytest inside the four `ProviderComplianceTests` methods
that use it. Shipped as **PR #259**, with two subprocess regression tests.

**Systemic point.** The existing guard test
(`test_all_public_names_importable.py`) was written for exactly this bug class —
its docstring says so — but only covered the `genblaze_core` top level, never
`genblaze_core.testing`. The lesson generalizes: the minimal-install CI job the
exec plan calls for should import **every public module**, not just every name in
the top-level `__all__`. A name can be reachable through the lazy table while its
documented module path is not.

---

### 4. B2 credentials diverge from Backblaze's own naming — **P1**

**Symptom.** We set `B2_KEY_ID` and `B2_APPLICATION_KEY` — the names Backblaze's
own tooling uses — and got:

```
ValueError: Backblaze B2 credentials missing. Set B2_KEY_ID / B2_APP_KEY
environment variables, or pass key_id= and app_key= explicitly to for_backblaze().
```

`for_backblaze()` reads `B2_APP_KEY`. `B2_ENDPOINT` is not read at all; the
endpoint is derived from region only.

**Impact.** This is the one item on the list that is *specifically* a Backblaze
problem: Genblaze is Backblaze's SDK, and it does not accept Backblaze's own
environment variable names. Every sample app has to carry a shim. The repo's own
notes say 9 of 10 sample builds hit it. The error message is good — it names the
variable it wants — which is the only reason this is P1 and not P0.

**Proposal.** Accept both pairs as aliases with documented precedence
(`B2_APPLICATION_KEY` wins over `B2_APP_KEY` when both are set, log at INFO), and
honor an explicit `B2_ENDPOINT` when provided, falling back to region-derived.
Purely additive, no deprecation window needed. Matches row P1-12.

We did not PR this because picking the precedence direction is a maintainer call
with downstream effects on the sample-app fleet.

---

### 5. The GMI Cloud audio modality is unreachable — **P1**

Issue #251: the TTS and music parameter allowlists omit the `text` / `lyrics`
field the API requires, so every audio call returns HTTP 400. Not independently
reproduced here (no GMI Cloud credentials), so it is listed on the strength of
the issue rather than our own traceback.

**Proposal.** Beyond the one-line allowlist fix: allowlists that silently drop a
*required* field are a recurring shape. Consider having the param normalizer log
at DEBUG every key it drops. A user staring at a 400 can then see "we removed
`text` before sending" in one run with `GENBLAZE_LOG_LEVEL=DEBUG`, instead of
reading connector source.

---

### 6. `@dataclass` on a provider subclass breaks it silently — **P1**

**Symptom.** The idiomatic modern-Python way to write a small provider:

```python
@dataclass
class MyProv(SyncProvider):
    api_key: str = "k"
    @property
    def name(self): return "myprov"
    def generate(self, step): return []

MyProv()              # constructs fine
provider.invoke(step) # AttributeError: 'MyProv' object has no attribute '_retry_policy_override'
```

**Verified** on `genblaze-core` 0.3.8; the traceback surfaces in
`providers/base.py:515`, reached from `invoke()` at line 1828.

**Root cause.** `@dataclass` generates an `__init__` that overwrites the
inherited one, so `BaseProvider.__init__` never runs and the instance attributes
it sets (`_retry_policy_override`, `_poll_cache_max_age`, …) are missing. The
object then looks healthy for as long as nobody touches them.

**Impact.** The distance between cause and symptom is the problem. The user's
mistake is on line 1 of their file; the exception is 1300 lines deep in someone
else's, naming a private attribute, at runtime, possibly in production. There is
nothing in the traceback that points back at `@dataclass`.

It is worse than it first looks. The *first* failure is
`AttributeError: '_poll_cache_max_age'` inside `_attempt_once`; `invoke()`
catches it, and its error handler then touches `self.retry_policy` and raises a
second `AttributeError` on `_retry_policy_override`. So the message the user
reads is not even the thing that broke first. `invoke()`'s error path should not
be able to fail while reporting a failure.

**Proposal.** `BaseProvider.__init_subclass__` can detect it cheaply — if the
subclass defines its own `__init__` and `dataclasses.is_dataclass(cls)`, raise a
`TypeError` at class-definition time with the fix spelled out ("providers cannot
be dataclasses; `BaseProvider.__init__` must run — use a plain `__init__` and
call `super().__init__()`"). Failing at import beats failing at invoke.

A weaker alternative, if a hard failure is judged too aggressive: promote the
lazily-initialized attributes to class-level defaults so a missing
`__init__` degrades to default behavior instead of `AttributeError`. That is
more forgiving but hides the bug, so we would not recommend it alone.

We did not PR this — whether to hard-fail at class definition is a policy call —
so it is filed as **issue #261** with both routes written up.

---

### 7. Configuration has three homes and no map — **P2**

**Symptom.** `Pipeline.run(cache=...)` raises:

```
TypeError: Pipeline.run() got an unexpected keyword argument 'cache'
```

Caching is configured fluently: `.cache(StepCache(dir))`. Nothing in the error
says so.

**The broader shape.** Options live in three places with no rule a user can
predict:

- constructor — `Pipeline("name", preflight=..., project_id=...)`
- fluent builders — `.cache(...)`, `.step(...)`, `.config({...})`
- `run()` kwargs — `sink`, `timeout`, `max_retries`, `raise_on_failure`,
  `fail_fast`, `progress`, `on_progress`, `on_step_complete`, `on_retry`,
  `pipeline_timeout`

`timeout` is a `run()` kwarg; `cache` is a builder. Both are "how this run
behaves". The user has no way to guess which is which, so the loop is
guess → `TypeError` → grep the source.

**Proposal.** Two cheap things, in order:

1. Give the run entry points an explicit `**kwargs` catch that raises a
   `TypeError` naming the right call site for known-misplaced options.
   This is a small dict of redirects and turns the worst moment in the API
   into a self-solving one — the highest value-per-line change on this list.
   Shipped as **PR #260**, covering `run`, `arun`, `batch_run`, `abatch_run`
   and 11 misplaced options.
2. Put a single "where does each option go" table in
   `docs/features/pipeline.md`. Included in the same PR, built from the live
   signatures.

We deliberately did **not** PR the `cache=` alias itself. Accepting the kwarg
(versus only improving the error) changes the API surface, the repo's plan
already scopes it as an alias decision for Wave 3B, and it would widen the
three-places problem rather than narrow it.

A related asymmetry surfaced while writing that table and is worth fixing
separately: `progress` and `on_retry` exist on `run()`/`arun()` but not on the
batch entry points, and `max_concurrency` exists everywhere except `run()`.
None of that is documented. Making the four signatures converge — or
documenting why they cannot — would remove a second guessing game layered on
top of the first.

---

### 8. A successful fallback erases the failed primary from provenance — **P2**

Issue #239. Provenance is Genblaze's differentiator; "what we tried and it
failed" is part of what happened, and an audit trail that only records the
winning attempt cannot answer "was this output produced by the model we
intended?". Worth treating as a correctness bug in the product's core promise
rather than a logging nicety.

**Proposal.** Record every attempt on the step with its outcome, and keep the
existing accessor returning the successful one so nothing breaks. Additive.

---

### 9. Nothing gates docs and examples against the real signatures — **P2**

Items 2, 3 and 7 are all instances of one root cause: `examples/` and the code
blocks in `docs/features/*.md` are not executed by anything. `make lint` runs
`ruff format --check` over markdown code blocks — so formatting is gated, but
correctness is not. That is the wrong half.

We smoke-ran all 30 files in `examples/` against dummy credentials and
classified failures by exception type. It took one command, and it separates
"needs a real API key" from "the example is wrong" cleanly, because credential
failures raise `ValueError` from the connector while API drift raises
`TypeError` / `AttributeError`.

**Proposal.** A `make docs-check` target, wired into CI, that:

1. runs the local-only examples (`quickstart_local`, `agent_loop_local`,
   `streaming_local`) end to end — these need no keys and all three pass today,
   so the target starts green;
2. imports every other example and asserts its genblaze calls type-check against
   live signatures, or runs it with dummy keys and fails only on
   `TypeError`/`AttributeError`/`ModuleNotFoundError`;
3. extracts python blocks from `README.md`, `libs/*/README.md` and
   `docs/features/*.md` and does the same.

This is a day of work and it retires an entire recurring category — including
row P3-15, which currently reads as a manual one-shot audit that will need
redoing after the next signature change.

---

### 10. Version story — **P3**

`pip install genblaze` gives you umbrella 0.4.5 / core 0.3.8, while `main` is the
v0.7.0 wave. The wave tag looks like a package version, the packages move
independently, and PR #255 has already documented the trap. Issue #254 asks for a
compatibility table, which is the right answer.

One addition: emit the resolved package versions in the manifest's tooling
metadata if they are not already there. When a user reports a bug, "umbrella
0.4.5 / core 0.3.8 / s3 0.3.6" in the manifest removes a whole round-trip.

---

## 4. What we filed upstream

| PR | Item | Contents |
|---|---|---|
| [#258](https://github.com/backblaze-labs/genblaze/pull/258) | 2 | `PromptTemplate` accepts a positional template; new `test_prompt_template_positional.py` (9 tests, 7 fail on `main`); docs + CHANGELOG |
| [#259](https://github.com/backblaze-labs/genblaze/pull/259) | 3 | Lazy `pytest` import in `genblaze_core.testing`; 2 subprocess regression tests; CHANGELOG |
| [#260](https://github.com/backblaze-labs/genblaze/pull/260) | 7 | Misplaced run kwargs name the call site that works; 20 tests (12 fail on `main`); config-location table in `docs/features/pipeline.md`; CHANGELOG |
| [#261](https://github.com/backblaze-labs/genblaze/issues/261) (issue) | 6 | `@dataclass` provider footgun, with the double-`AttributeError` detail and three fix routes ranked |

All three follow `CONTRIBUTING.md` and the PR template, pass `make test`,
`make typecheck` and `make deptry` locally, and reference the matching
`docs/exec-plans/` rows. No CLA was required — `CONTRIBUTING.md` only states
that contributions are MIT-licensed.

One process note: CI on all three sits at `action_required`, GitHub's default
for a first-time contributor's fork. A maintainer has to approve the workflow
runs before any check reports. Nothing to fix on our side, but it does mean the
PRs look check-less until someone clicks.

---

## 5. The one-paragraph version

Genblaze's core bet — manifests and provenance — is good, and the local paths
work: the three key-free examples run clean and produce verifiable manifests on
the first try. What is weak is the first hour. The shipped batch example crashes
on its own first line, the zero-key quickstart in the README cannot be imported
on a clean install, the SDK's own credentials do not match Backblaze's
environment variable names, and the preflight check that runs by default reports
the wrong answer. None of these are deep; all of them are things a new user hits
before they have any reason to trust the library. The cheapest structural fix is
to make CI execute the things that make promises — examples, README snippets,
minimal installs — because every P0 in this document would have been caught by
that one job.
