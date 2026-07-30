# Evolution Log — omlx

The loop that turns real use into skill improvements. Mechanism:
`../core/references/evolution-loop.md`. Harvest source: real traces from projects that call a
local oMLX server, plus live probes against a running install.

**Standing rule for this skill:** every non-obvious behavioural claim carries the oMLX version
and date it was verified. Two claims have already changed between releases (see Evolution 1),
so an unstamped assertion here is a liability, not knowledge. A contradicted claim gets
corrected **with the command that reproduces the correction**.

---

## Evolution 1 — 2026-07-29/30 — birth, from six codebases and a live teardown

### Harvest scope

Three parallel mining passes: (a) the heaviest single consumer — a recruiter/GUI-automation
pipeline with a wrapper, VLM grounding, and a model-consolidation study; (b) a sweep of every
other project touching a local MLX endpoint — a document-OCR pipeline, a literature-graph
pipeline, a Rust agent harness, a client model-selection benchmark, an MLX quantization lab;
(c) a read-only teardown of the live install (config, CLI, `openapi.json`, logs, stats).

Classify: **authored** — every claim traces to a real failure someone paid for, not to
documentation. But the *generalization* across projects is new, so the verdict is still
`PENDING` until the skill itself is used on unfamiliar work.

### Patterns found

Ranked (Impact ÷ Effort), with the proof each bit:

1. **The same lesson was re-derived in isolation 3+ times.** `response_format.type` defaulting
   to `"text"` (silent unenforced output) was independently discovered and written down by
   three separate projects, each paying full debugging cost. Impact: H, Effort: L. This is the
   entire case for the skill existing.
2. **Every oMLX failure mode is silent.** Enforcement no-ops, thinking truncates, SpecPrefill
   drops 80% of a prompt, MTP falls back, batching perturbs greedy decoding, memory guard
   stalls. Not one of them raises. Impact: H, Effort: L → became principle 2 and the shape of
   both scripts.
3. **The cross-project reference had already gone stale.** The one shared notes file asserted a
   32K context cap (actually a dead fallback; effective window is model-native), TurboQuant
   "ON by default" (off on every model), and an oMLX version two minors behind. Impact: H,
   Effort: M → became the config-first grounding gate.
4. **Nobody had a pre-flight check.** Every project discovered a cold load, a wrong model id,
   or a starved engine *during* a long run. Impact: M, Effort: M → `omlx_probe.sh`.
5. **Everybody hand-rolled the same 60-line client** and they drifted only on retry coverage.
   Impact: M, Effort: L → `omlx_client.py`.

### Hypotheses applied

1. `SKILL.md` + five references (`request-contract`, `serving-ops`, `performance`,
   `model-selection`, `eval-playbook`) — closes patterns 1–3.
2. `scripts/omlx_probe.sh` (+ `--deep`, `--canary`) — closes pattern 4.
3. `scripts/omlx_client.py`, `scripts/omlx_batch.py` — closes pattern 5.
4. Output Contract stated as a three-part proof (request shape + no `Warning` + parsed keys)
   rather than a slogan — closes pattern 2 at the gate level.

### Claims corrected before shipping (by test, not by inheritance)

Four inherited claims were falsified by negative control against oMLX **0.5.3**. Recorded
here because *how* they were wrong is the reusable lesson:

| Claim as inherited | Reality on 0.5.3 | How it was caught |
|---|---|---|
| `response_format.type` **and** `name` are both load-bearing | `type` yes (reproduced); **`name` omitted still enforced**. It was load-bearing on 0.4.x. | negative control, 4 request variants |
| `structured_outputs={"json": schema}` is vLLM syntax, ignored here | **It enforces.** `guided_json` is the one that's ignored. | dogfood critic; re-verified independently with a schema demanding keys the model could not invent |
| thinking is ON by default | Engine default is ON; a **per-model setting overrides it**, and on this box both large models were pinned OFF. The default is not knowable from the request. | 3-way probe (absent / False / True) × 2 models |
| `tool_choice` is parsed and ignored | `"required"` ≡ `"auto"` (so it can't force a call), but **`"none"` does suppress**. | 4-way probe |

The pattern behind all four: **a claim about a silent behaviour cannot be inherited, only
re-run.** Hence the standing version-stamp rule at the top of this file.

### Dogfood (checklist step 6)

Fresh-context critic, briefed with `skill-builder/references/templates/dogfood-brief.md`, used
the skill on a real 12-item structured-extraction task against the live server (not a read —
it produced `extract.py`, `claims.py`, five timed runs, and byte-comparison output). Returned
12 findings, 4 at `high`. All folded back:

| Finding | Fix |
|---|---|
| `structured_outputs={"json":…}` documented as a no-op — it enforces | table + prose rewritten in `request-contract.md`, anti-pattern narrowed to `guided_json` |
| Output Contract satisfiable with enforcement *off* (parseable JSON + right keys + no Warning proves nothing) | contract now requires proof of the **request shape**; `chat_meta()` returns `meta["enforcement"]`; `chat_json()` refuses to run unconstrained |
| Probe passed a saturated server; the mandated throughput canary existed in no script | `--canary` added; scheduler saturation is now a FAIL, not an info line |
| 1.36× was the only concurrency anchor, and is 2.1× low for short prompts | two-row measured table (1.4× prefill-bound / 2.9× short-prompt), "1.4–3×, never 8×" |
| Uncompilable schema kills the connection with no status/body/Warning | `SchemaRejected` + the xgrammar failure taxonomy documented |
| `chat()` `KeyError: 'content'` on any tool-call response | `msg.get("content") or ""`, `tool_calls` exposed in meta |
| Probe silently showed *other* models' config when `--model` had no entry | explicit WARN, never substitutes |
| **No batch template — the single most important missing thing** | `scripts/omlx_batch.py` |
| Trigger collides with `claude-api` (its SKIP list names other providers, not "local"/MLX) | disambiguation clause added to frontmatter and hand-off section |
| No `EVOLUTION.md`; evolution loop not declared as a gate | this file; gate added |
| Probe noise on non-chat models; green-lit a non-LLM pseudo-model | warnings gated; pseudo-model flagged by null context |
| `grammar-constrained decoding is unavailable` missing from the log-grep table | added |

Critic-confirmed as working: the `Warning`-header mechanism (it tripped a real one with a
lookahead regex), config-before-probe ordering, the abstention-branch mandate (4/4 correct
abstentions, no prompt iteration), and greedy non-reproducibility under batching (12/12 vs
10/12).

Not modified (validated as already sound): the SpecPrefill / DFlash / MTP verdicts, the
context-budget reasoning, the local-vs-cloud routing rules, the eval-playbook ladder.

### Addendum — 2026-07-30 — Gemma-4 tool-calling template

Follow-up harvest of a client model-selection benchmark's function-calling investigation, which
the first pass had summarized only as "oMLX ignores `tool_choice`". Re-read at source and
verified against the shipped template on this box. Added to `request-contract.md` §Tool calling
and `model-selection.md`:

- oMLX renders tools through the **checkpoint's own `chat_template.jinja`** — inspectable in the
  model dir, so the wire format is knowable before you send anything.
- **Gemma-4's native tool syntax is not JSON** (`<|tool_call>call:NAME{k:<|"|>v<|"|>}<tool_call|>`,
  args comma-separated `key:value`, strings in a `<|"|>` special token). Verified present in the
  local template: 22 `<|"|>`, 1 `<|tool_call>`, 2 `<|channel>thought`.
- **The diagnostic**: grep missed responses for native markers to separate a parse bug from an
  elicitation failure. 0/52 in that audit → parser innocent, and the original "no parser,
  emulates via forced JSON" theory was wrong. Cheap test, redirects an entire investigation.
- Truncated calls strand `<|tool_call>call` in `content` with `finish_reason: length` — looks
  like a parse bug, isn't.
- Forcing calls via `response_format`/GBNF was tried and **rejected**: output lands in `content`
  not `tool_calls`, and it destroys refuse-case discrimination. Same shape as the abstention
  lesson.
- Invocation rates are flag-sensitive enough to reverse a model ranking (22→54 vs 43→49), and a
  gap that looked like it needed FC fine-tuning was a serving/config artifact.

Refinement, not conflict: the source says "oMLX ignores `tool_choice`", tested on
`auto` vs `required`. The 0.5.3 probe here found `none` *does* suppress, so the skill's narrower
claim ("`required` is not a forcing function") stands and is more precise.

### Validation results (fill after ≥2 independent real uses)

*(superseded by Evolution 2 below — several boxes closed there)*

- [ ] The three-part Output Contract is exercised on a real task and doesn't produce
      false-positive "unenforced" alarms.
- [ ] `omlx_batch.py` is used by a project that did **not** author it, without needing local
      modification.
- [ ] The four corrected claims are re-checked against the next oMLX release (they are the
      most likely to drift again).
- [ ] The concurrency table is confirmed or extended by a third workload shape.
- [ ] The `claude-api` disambiguation actually prevents a mis-fire on a real ambiguous turn.
- **Verdict: PENDING** — one authoring pass plus one dogfood, all on a single machine and a
  single oMLX version. The measured numbers are hardware-specific by construction, and four
  claims already changed between 0.4.x and 0.5.3. `KEEP` needs ≥2 independent real uses,
  ideally one on a different install.

---

## Evolution 2 — 2026-07-30 — second dogfood: three shipped claims falsified

### Harvest scope

A second fresh-context critic, briefed on a **deliberately different** task from the first
(embeddings + cosine retrieval ranking, then a typed verdict per (query, chunk) pair through
`omlx_batch.py` with a schema the script had never seen). Required because three `high`
findings from Evolution 1 forced *structural* changes — the Output Contract gained a third
condition, `chat()` split into `chat_meta()`, and `omlx_batch.py` did not exist when the first
critic ran. Per the authoring checklist, a structural change re-opens the gate.

15 findings, 4 at `high`. Every falsification below was **independently re-verified here**
before the docs were changed — the critic's evidence was the lead, not the proof.

### Claims falsified (the important part)

| Shipped claim | Reality on 0.5.3 | Evidence |
|---|---|---|
| "`max_tokens` caps `content` only — does **not** bound `reasoning_content`" (cited 8,091 tokens under a 2048 cap) | **False. It is a hard cap on total generation.** 8 trials, 3 models, zero over-runs. The real failure is the inverse: too small a budget lets reasoning eat the answer, `finish_reason: length`, partial CoT leaking into `content`. | `max_tokens` 32/100/512 → completion 32/100/200 |
| "the memory guard stalls instead of erroring. Nothing raises" (stated twice) | **False for the model-load guard.** HTTP **507**, immediate, 51/51, with fully actionable body text. Two guards exist; only the prefill one is silent. | server.log, reproduced live by the probe |
| "rerank 400s (not 404s) when no cross-encoder is loaded" | **Imprecise.** 400 for a served-but-wrong-type id, **404** for an id that isn't served. | direct probe of both |
| "accept the ~8× wall-clock cost" of concurrency 1 | **Overstated 5×** — and contradicted the same file's own new 1.4–3× table. Measured 1.48×. | 4 timed runs |
| cold load ~0.9 s/GB | **Overstated 2–3.5×** — 0.26–0.44 s/GB measured. Now given as a range with "time your own". | 3 loads |
| "a clean call is `content == \"\"`" | **Wrong about the wire** — the key is *absent*; `.get()` returns `None`. | raw message keys |

Also: the memory **ceiling is not a constant** (70.25 / 70.71 / 70.72 / 70.91 / 71.13 GB in one
session, while `/api/status` said 78.5 and 83.5 GB) — the reference's "~78 GB" is now flagged
as moving.

### Self-inflicted regressions from Evolution 1's own fixes

The pattern worth naming: **three of the four `high` findings were caused by the previous
round's fixes**, not by the original draft. Fixing under time pressure without re-reading the
neighbours is how a skill acquires internal contradictions.

1. **`--canary` — added to close "no throughput canary" — committed the skill's own
   warm-vs-cold anti-pattern.** It printed `WARN model is NOT loaded`, then timed the cold
   load and labelled it `(warm)`: **13.9 tok/s vs 60.9 tok/s** for the identical command 60 s
   later. Fixed: the canary now loads first (reporting load time separately) or fails.
2. **The Output Contract's condition 1 was unreachable from the entry point the flow table
   recommended.** It mandated recording `meta["enforcement"]`; the flow table routed to
   `chat_json()`, which returns a bare dict. The script `omlx_batch.py` used `chat_meta`
   internally — i.e. the skill's own tooling didn't follow the skill's own routing. Fixed:
   `chat_json(..., with_meta=True)` and the flow table updated.
3. **`performance.md`'s new measured table sat ~25 lines above a surviving "~8× cost".**
4. **`omlx_batch.py`'s audit trail recorded `enforcement` but dropped `warnings`** — so
   condition 2 could only be *inferred* from the absence of an exception, which is exactly the
   inference-from-activity the contract forbids.

### Hypotheses applied

Client: `ServerRefused` carrying the server's message (a bare `HTTPError: 507 Insufficient
Storage` threw away the actionable half); retry only 408/429/502/503/504 — never a 507, which
just re-pressures an out-of-memory box; `chat_json` refuses unconstrained **pre**-flight
(was post-flight, burning the inference first — 2.8 s → 0.000 s); `with_meta`; `embed()` gained
the four documented fields it couldn't send, plus the silent-truncation and query/passage
asymmetry warnings.

Batch: `warnings` + `ts` + `idx` + a cached `/api/status` `server` stamp per record; incremental
flushed writes (a Ctrl-C at item 199/200 no longer loses the trail — records are now in
completion order, `idx` recovers pairing); `format_map` so multi-field items don't smuggle
everything through one `{item}` blob; `check_abstention` now requires the key be **boolean**
(a string enum would coerce to "never abstained" forever) and warns against conflating the
abstain key with the verdict field.

Probe: warm-before-timing, saturation FAIL now aborts before `--canary`/`--deep` instead of
producing confounded numbers.

Docs: all six falsifications above; the concurrency-1 rule **scoped** (bytes/free-text → 1
worker; a bounded field you branch on → verify field stability at your real worker count, per
`eval-playbook.md`'s own "measure what you branch on"); an ad-hoc escape hatch in the Output
Contract; a "no hits" reading for the `chat_template.jinja` grep.

### Validation results

Closed by this round:
- [x] **`omlx_batch.py` used by someone who did not author it, on a foreign schema and task** —
      unmodified, first try, 15/15 enforced, tp=3 fp=0 fn=0 tn=12, 2/2 correct abstentions on
      adversarial input. Two friction points found and fixed (single placeholder; abstain-key
      conflation).
- [x] **Third workload shape for the concurrency table** — mid-size structured ≈ **1.48×**,
      between the 1.4× and 2.9× rows.
- [x] **Independent replication of the batching non-determinism claim**, structural detail
      included (15/15 at w1, 13/15 at w8, optional payload fields dropped).
- [x] **The structured-output table verified in full**, all six variants, by discriminating
      negative control.

Still open:
- [ ] The three-part Output Contract exercised without false-positive "unenforced" alarms —
      partially shown (0 false alarms across 19 enforced items), needs a run where enforcement
      genuinely degrades mid-batch.
- [ ] The six corrected claims re-checked against the next oMLX release. **Three of them
      changed between 0.4.x and 0.5.3, so treat this list as the most volatile content here.**
- [ ] The `claude-api` disambiguation preventing a real mis-fire.
- [ ] Any use on a **different machine or oMLX version** — every number here is from one box.
- **Verdict: PENDING.** Two dogfoods on one machine is not two independent real uses. The
  falsification rate across two rounds (6 claims in round 2, 4 in round 1) is itself the
  argument for keeping it PENDING: this skill's content decays faster than its structure.
