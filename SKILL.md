---
name: omlx
description: "Call the local oMLX server (OpenAI-compatible MLX on Apple Silicon) correctly, and know when not to. Covers JSON-schema enforcement, thinking control, server ops, batching/MTP/SpecPrefill verdicts, model choice, vision grounding, and proving a local model is good enough. USE WHEN calling a local MLX/oMLX endpoint, forcing structured JSON from a local model, debugging one that rambles/truncates/ignores a schema, tuning local throughput or concurrency, picking a served model, or deciding local-vs-cloud for a role. Owns LLM turns whose provider is local/MLX/oMLX — not `claude-api` (Anthropic-hosted), not `dev` (builds the calling software); training/fine-tuning is out of scope. Keywords: oMLX, local LLM, MLX, Apple Silicon, local inference, OpenAI-compatible, json_schema, response_format, structured output, enable_thinking, thinking_budget, chat_template_kwargs, embeddings, rerank, VLM, GUI grounding, MTP, TurboQuant, SpecPrefill, DFlash, prefix cache, continuous batching, 127.0.0.1:8000."
license: MIT
---

# oMLX — the local model, used correctly

Produces working calls against a local oMLX server and the evidence that they worked: typed
JSON that was actually schema-enforced, throughput numbers measured warm, a model choice that
survives its own eval, and an honest verdict on whether the local model should own the role at
all. Distilled from six independent projects that each re-derived the same expensive lessons
in isolation.

> Inherits the `core` kernel — see `../core/SKILL.md`. Obey its Integrity Constraints;
> declare its gates; reference its files, don't copy them.

## When it fires — and when a sibling owns it

- **This skill:** any call to a local oMLX/MLX endpoint; structured-output and thinking
  control; "the local model ignored my schema / rambled / truncated / is slow"; concurrency
  and cache tuning; server lifecycle and model management; picking among served models;
  vision/GUI grounding; local-vs-cloud role routing; proving a local model is good enough.
- **`dev` instead:** building or changing the software *around* the call — the pipeline, the
  service, the tests. This skill decides *how to call*; `dev` builds *what calls*.
- **Training/fine-tuning instead:** producing a checkpoint (SFT/DPO/GRPO) is out of scope —
  this skill serves and consumes checkpoints, it does not make them. Note the escalation
  ladder in `references/eval-playbook.md` puts fine-tuning *last*, and in practice steps 1–6
  have resolved every case so far.
- **`claude-api` instead:** the same LLM-shaped task (extract / classify / summarize, tool
  use, agent loops) against **Anthropic-hosted** models. Both skills trigger on "extract typed
  JSON from these documents"; the tiebreaker is **which endpoint runs it**. Local / MLX / oMLX
  / "the local model" → here. Claude / Anthropic / an API key → `claude-api`.
- **Project `wiki/` instead:** which prompts this project uses, what's blocked, what shipped.
  This skill is the transferable method, not one project's memory.

> **Disambiguation:** artifact = a request/response that must be *correct against this server*
> → omlx. Artifact = a code change, a pipeline, a test suite → `dev`. Artifact = model weights
> → out of scope. Same artifact but a hosted Anthropic model → `claude-api`.

**Other local backends** (in-process `mlx_lm`, LM Studio, a remote `llama.cpp`) are out of
scope. Reach for them only when oMLX genuinely can't serve the architecture, or when you need
a single-process library rather than a server; otherwise prefer the one endpoint with the
batching scheduler and the prefix cache.

## Principles / anchoring rules

1. **Config-first, then probe.** `~/.omlx/settings.json` and `model_settings.json` decide
   whether half the documented request fields do anything. Read them before experimenting — a
   recorded trace shows black-box guessing producing "a confident, wrong, fabricated-causality
   conclusion" that a second investigation had to unwind.
2. **Almost every oMLX safety knob fails silently rather than loudly.** Schema enforcement
   no-ops, thinking truncates the answer, SpecPrefill drops most of your prompt, MTP falls
   back, batching perturbs greedy decoding, embedding inputs get cut, the *prefill* memory
   guard stalls instead of erroring. (The honourable exception: the **model-load** memory
   guard returns a loud, actionable HTTP 507 — don't generalize the silence to it.)
   So **verify the artifact, never the activity** — a 200 response is not evidence that
   anything you asked for happened. And for enforcement the artifact includes *what you sent*:
   an unconstrained model returns well-formed JSON with your exact keys, so the response alone
   can never tell you a grammar ran (see the Output Contract).
3. **Two fields decide whether you get an answer at all — never let either default.**
   `response_format.type` defaults to `"text"`, so omitting it silently disables enforcement
   (reproduced by negative control on 0.5.3). `chat_template_kwargs.enable_thinking` defaults
   ON *at the engine*, but a per-model setting can override it either way — so the default is
   not knowable from the request. Send both explicitly on every call.
4. **A schema forces shape — never content, and never abstention.** A forced `{x,y}` invents
   coordinates for an off-screen target. Give every perception/lookup schema a `found:false` +
   `reason` branch, and fence content in code (banned substrings, length caps) for anything a
   human reads.
5. **One heavy model hot at a time.** Two ~36 GB models on a 96 GB machine means eviction
   thrash and a ~36 s cold reload *between every stage*. Batch all work for model A, then all
   for model B — never interleave.
6. **Concurrency is a decode lever, not a prefill lever.** At 8 workers: **~1.4×** on long
   prefill-bound prompts, **~2.9×** on short-prompt structured work, **~1×** for images (they
   prefill individually). Budget 1.4–3×, never 8×. Batching also breaks greedy
   reproducibility — 12/12 identical records sequentially vs 10/12 at 8 workers, and it has
   already manufactured one phantom improvement. Drop to 1 worker when you compare **bytes or
   free text**; if you only branch on a bounded field, verify *that field's* stability at your
   real worker count instead of paying the slowdown blind.
7. **Local is free, not free of consequence.** Route by role: volume/mechanical → cheap cloud;
   judgment → local; a judge may never be a contestant in its own benchmark; never mix
   extraction models within one corpus.
8. **Measure the thing you branch on.** One model failed on score-MAE and passed 6/6 on the
   decision the pipeline actually consumed. Both numbers were right; only one was the
   instrument.

## The flow (at a glance)

| You need | Do this | Depth |
|---|---|---|
| Any call at all | `scripts/omlx_probe.sh --model <id>` first — server up, key valid, model **loaded**, scheduler not saturated, and the flags that silently change behaviour. Add `--canary` before a long run, `--deep` to prove enforcement. **Not a `curl /v1/models`** — that returns 200 with every model listed while nothing is loaded, so it cannot see the cold load it is supposed to warn you about | `references/serving-ops.md` |
| Typed JSON out of a local model | `omlx_client.chat_json(..., with_meta=True)` with a schema **and** an abstention branch — `with_meta` is what returns the enforcement proof the Output Contract asks you to record | `references/request-contract.md` |
| The same over N items | `scripts/omlx_batch.py` — enforcement proven per item, abstention branch required, JSONL audit trail out | `references/eval-playbook.md` |
| The model rambled / truncated / ignored the schema | thinking flag, then `response_format.type` + `name`, then `max_tokens` headroom | `references/request-contract.md` |
| Server won't answer, or is 5× slow | zombie-port check, cold-load, model starvation, memory guard | `references/serving-ops.md` |
| Faster | prefix cache first (49 s → 1.7 s), then concurrency, then MTP. Not DFlash, not SpecPrefill | `references/performance.md` |
| Which model / local vs cloud | roster by role + routing rules; certify model *and* thinking flag together | `references/model-selection.md` |
| Screenshot / GUI grounding / VQA | crop to near-square, greedy sampling, abstention branch, one-word VQA | `references/model-selection.md` §Vision |
| "Is the local model good enough?" | golden set + same-model baseline; hand-read the misses | `references/eval-playbook.md` |
| Model won't call my tool | read the checkpoint's own `chat_template.jinja`; grep misses for native markers to rule out the parser; then thinking ON + token headroom | `references/request-contract.md` §Tool calling |
| Embeddings / rerank / STT / TTS | request shapes; embedding inputs are **silently truncated** by default | `references/request-contract.md` |

## Gates (declared, inherited from core)

- **Grounding gate** — substrate, in this order: (1) live config (`~/.omlx/settings.json`,
  `~/.omlx/model_settings.json`), (2) the running server (`GET /openapi.json`, `/v1/models`,
  `/api/status`), (3) these references. Record one line:
  `Grounded: <file|endpoint> → <finding>`. Documented behaviour that contradicts live config
  loses. See `../core/references/grounding-gate.md`.
- **Output Contract** — done = the output **plus proof enforcement actually ran**, which takes
  *three* checks, not two:
  1. **the request carried a real constraint** — `response_format.type == "json_schema"` (or
     `structured_outputs.json`) was actually sent. Necessary because an unconstrained model
     asked politely for JSON returns parseable JSON with all the right keys; response-side
     checks alone will green-light it.
  2. **no HTTP `Warning` header** — oMLX's signal that it degraded to prompt coaxing.
  3. **it parsed and the required keys are present** — kept even under enforcement.

  Use `chat_json(..., with_meta=True)` or `chat_meta()` — both return `meta["enforcement"]`
  (1) and `meta["warnings"]` (2). **Record both**, don't just observe them: "no exception was
  raised" is inference-from-activity, which is the exact proxy this contract exists to forbid.
  `scripts/omlx_batch.py` writes both into its JSONL per item. A 200 and a plausible-looking
  object is a proxy, not an artifact.

  New oMLX behaviour learned gets written back into the relevant reference here, and the run's
  findings into the project's `wiki/` — or, for ad-hoc work with no project, stated in the
  response and, if behaviour was learned, added to `EVOLUTION.md`. See
  `../core/references/wiki-protocol.md`.
- **Evolve from real use** — every non-obvious behaviour this skill asserts is dated and
  version-stamped, because two of them have already changed between oMLX releases. A
  contradicted claim gets corrected *with the reproducing command*, and the correction is
  recorded in `EVOLUTION.md`. See `../evolution/references/loop.md`.
- **Pushback & teach** — challenge "just point it at the local model" when the workload is
  coverage-critical (SpecPrefill and context truncation silently drop input),
  reproducibility-critical (batching breaks greedy determinism), or needs an impartial judge
  (the local model may be a contestant). Say which one applies and what it would cost. See
  `../core/references/pushback-and-teach.md`.
- **Approach declaration** — one line before substantive work: model · thinking on/off ·
  structured or free · workers · expected cold-load cost. Skippable for a single ad-hoc call.
  (core SKILL.md §7.)

## Anti-patterns (hard no-list)

- `response_format` without `type: "json_schema"` — silently unenforced. (Send `name` too: the
  spec requires it and its behaviour has already changed across versions.)
- `guided_json` — accepted and silently ignored (it's vLLM syntax). Note `structured_outputs=
  {"json": schema}`, which *looks* equally vLLM-ish, **does** enforce here; `guided_json` is
  the one that does nothing.
- Concluding "enforcement is on" from a parseable response. An unconstrained model returns
  well-formed JSON with your exact keys if you ask nicely. Check what you *sent*.
- `/no_think` or `/think` in the prompt — ignored on this build.
- Setting a huge `max_tokens` on the theory that it won't bound reasoning anyway. On 0.5.3 it
  **does** bound total generation — so the real risk is the inverse: too small a budget with
  thinking on lets the reasoning eat the answer and returns a truncated ramble at
  `finish_reason: length`.
- A tight-but-nonzero `thinking_budget` — it relocates the spiral into `content` instead of
  saving anything. Use 0 / `enable_thinking:false`, or give it real room.
- Sending `thinking_budget` without checking `thinking_budget_enabled` + `reasoning_parser`
  server-side — otherwise it does nothing.
- A forced schema with no abstention branch on a perception or lookup task.
- Trusting `tool_choice: "required"` to force a call — it behaves identically to `"auto"`.
  (`"none"` does work.)
- Forcing a tool call with `response_format` or a grammar — it lands in `content` instead of
  `tool_calls`, and removes the model's ability to decline.
- Blaming the tool parser before grepping the missed responses for native markers.
- Assuming a model's tool format is JSON. Read its shipped `chat_template.jinja`.
- Promising 8× from concurrency, or quoting a single speedup number for all workloads;
  quoting per-request-under-load tok/s as headline speed; benchmarking warm against cold;
  taking any timing while another tenant holds the scheduler slots.
- Claiming temp-0 reproducibility under continuous batching.
- Enabling SpecPrefill for coverage tasks (extraction, audit, summarize-for-completeness).
- Enabling DFlash on a fan-out workload — it evicts the batched engine.
- Interleaving two large models per item instead of batching by model.
- `omlx restart` while the GUI app owns the server; symlinked model directories.
- Treating `omlx diagnose` as a health check (it isn't) or `sampling.max_context_window` as a
  cap (it's a dead fallback when the model reports native context).
- Trusting a personal notes file over live config. The notes drift; the config doesn't.

## Composition

- **Inherits:** `core`.
- **Consumed by:** `dev` (any build whose pipeline calls a local model), and any project doing
  local classification, extraction, scoring, grounding, or embeddings.
- **Hands off to:** whatever training stack you use, when the answer really is a better
  checkpoint — which `references/eval-playbook.md` puts *last* on the ladder, for good reason.
- **Origin:** mined 2026-07-29 from six independent codebases (a recruiter/GUI-automation
  pipeline, a document-OCR pipeline, a literature-graph pipeline, a Rust agent harness, a
  client model-selection benchmark, an MLX quantization lab) plus a live teardown of the
  running server. Fire-tested there, generalized here.

## References

- `references/request-contract.md` — the wire: endpoints, the full chat field list and what's
  absent, structured output (three non-interchangeable knobs, the silent no-op, the `Warning`
  degradation path, abstention schemas), thinking control and the elasticity table, sampling,
  multimodal payloads, server-side message rewriting.
- `references/serving-ops.md` — config-first gate, the CLI, lifecycle traps (GUI ownership,
  zombie port race, cold load, idle unload), model management and id conventions, the separate
  admin auth realm, memory guard and the wired-memory ceiling, log grep recipes.
- `references/performance.md` — concurrency reality and the 1.36×, greedy non-reproducibility
  under batching, the prefix cache as the #1 lever, verdicts on MTP / TurboQuant / SpecPrefill
  / DFlash, context budget (the binding ceiling is quality, not memory), warm-vs-warm
  benchmarking, reference throughput.
- `references/model-selection.md` — roster by role and its traps, model choice entangled with
  the thinking flag, local-vs-cloud routing rules, and vision/GUI-grounding discipline.
- `references/eval-playbook.md` — measure what you branch on, the golden-set gate and the
  same-model baseline trap, replay harness shape, when calibration backfires, per-field
  reliability, audit trails as continuous eval, the escalation ladder.
- `scripts/omlx_client.py` — stdlib-only client: schema enforcement wired correctly, thinking
  sent explicitly, `Warning`-header detection, `meta["enforcement"]` reporting what was sent,
  required-key validation, refusal of the silent near-misses (`guided_json`, uncompilable
  schemas, unconstrained `chat_json`), transient-only retry, order-preserving fan-out, CLI
  smoke test.
- `scripts/omlx_batch.py` — schema-enforced batch extraction over N items: mandatory
  abstention branch, per-item enforcement proof and timing, JSONL audit trail, worker count
  matched to scheduler capacity.
- `scripts/omlx_probe.sh` — pre-flight gate: server, auth, loaded-vs-cold models, scheduler
  saturation, and the silent-behaviour flags. `--canary` measures sustained decode before a
  long run; `--deep` proves schema enforcement is actually live.
