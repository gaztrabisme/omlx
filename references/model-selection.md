# Choosing the model — and choosing local at all

## The roster, by role

Get the truth from `GET /v1/models`, never from memory. What a typical install carries:

| Role | Shape | Notes |
|---|---|---|
| General text / judgment | a 30–40B MoE at ~8-bit (~36 GB) | the workhorse; 256K-native context |
| Vision + GUI grounding | a VLM of the same family/size | see §Vision below |
| Small/fast text | a 4–12B at 4-bit (~7–9 GB) | dispatch, classification, cheap loops |
| Embeddings | a small retrieval encoder (~1 GB, 1024-dim) | `/v1/embeddings` |
| Rerank | a cross-encoder | **often not installed** — `/v1/rerank` then **400s, not 404s** |
| Doc conversion | a non-LLM shim exposed as a "model" | do not send chat to it |

Traps: hidden models don't appear in one of the two listing endpoints; HuggingFace-cache
models use `org--name` ids; a model id can change under you when a variant is re-quantized
(e.g. gaining an `-mtp` suffix), and the old id simply 404s.

## Picking

**Size is not the sort key — task class is.** A 12B at 4-bit at ~35 tok/s beats a 35B at
~2 tok/s-under-load for anything you're running thousands of times, if it passes your eval.

**Model choice and the thinking flag are entangled — certify them together.** Measured on a
100-case agentic function-calling benchmark: with thinking OFF the larger model won; with
thinking ON the *smaller* model won (0.625 vs 0.614). Tool-invocation rates were even more
flag-sensitive: 22/100 → 54/100 for the small model, 43/100 → 49/100 for the large one, which
**reverses which model triggers tools more**. Ranking two models under one flag setting and
then shipping the other is how a benchmark lies to you. Once decided, **hardcode the flag** — a
runtime knob is how "certified" and "shipped" drift apart.

Corollary worth its own line: a capability that looks like a *model* deficit ("the small model
barely calls tools — we'll need FC fine-tuning") was almost entirely a **serving-path and
config artifact**. The answer-quality gap between the two models, meanwhile, was invariant
across serving stacks (~0.057 either way) — that one was real. Separate the two before
budgeting a fine-tune.

**MoE beats dense at the same parameter count on this hardware.** In a 100-row generation
sweep, MoE models at 30–80B ran 32–43 tok/s while a dense 14B managed 14.7 (a 4073 s pipeline
vs 380–770 s for the MoEs).

**Small local models need extractive-only grounding.** Asking a mid-size local model to select
quotes "supporting" an *absence* triggers its refusal / empty-output path. Ask for what is
present; do the negation in code.

**Proposer family is not fungible.** On identical validated gauntlets, one frontier model
produced 10/28 accepted proposals where a local 35B produced 1/23 — and the local model went
1 → 10 once given retrieval tools. "LLM proposer" is not one thing; neither is "add tools".

## Local vs cloud — routing rules

Local is free and private, not free of consequence.

| Route to **local** | Route to **cloud** |
|---|---|
| Judgment / adjudication / critique | High-volume mechanical extraction (17× faster, cents) |
| Anything on a discard path (probes, throwaway exploration) that must never bill | Anything needing a *different model family* for independence |
| Privacy-bound content | Long-horizon agentic work needing frontier reasoning |
| Summarization of decaying state / memory compaction | |

Hard rules learned the expensive way:

1. **A judge may never be a contestant.** If the local model is one of the models being
   ranked, the LLM-judge must be a different family, hosted elsewhere.
2. **Never mix extraction models within one corpus.** Model-correlated extraction density is
   the same poison as sparse prefill. If you switch, re-extract everything uniformly.
3. **Cheap and clean is not the same as correct.** A cloud model that was mechanically perfect
   (0 errors, 17× faster, ~$0.05) produced one specious refutation whose false reasoning passed
   a provenance-only verifier. The judgment role stayed local; the volume role moved.
4. **Best answer is often an ensemble across families**, with a third family auditing.
5. Before adding a live local dependency to a hot path, check the *marginal* gain. One dense
   retrieval leg measured 14/14 vs 13/14 for plain lexical search — and shipped **lexical**,
   because 1/14 didn't justify a live inference dependency on the recall path.

## Vision & grounding

GUI-grounding VLMs return coordinates **normalized to `[0, 1000]` over the image you actually
sent** — map back to logical points using the frame extent you cropped from, not the screen.

**Aspect ratio is load-bearing.** These models train on near-square images. Grounding on a
full ultrawide (32:9) frame was fuzzy — ~36 px off a large, unambiguous target. Cropping to a
single application window (aspect 3.56 → 1.21) made it precise. **Crop to the region of
interest before you ask**, and upscale small regions.

**Crop is also a safety measure.** A verification check was once fooled by content visible
*behind* the target window. A tight crop of the exact band you care about (plus a 3× upscale)
removes the distractor rather than hoping the model ignores it.

**Residual scatter is ~40 px on complex layouts.** A re-ground is close to an independent
trial, so **retry beats prompt-tuning** for scatter. Prompt-tuning is for systematic error.

**Sampling matters more than usual.** Greedy (temp 0) gave 12/12 hits, median 3 px error. The
model's own instruct preset (temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5) gave
11/12, median 27 px, ±40 px jitter. `presence_penalty` on a two-integer output is actively
harmful. Send explicit sampling params for grounding.

**Always give it an abstention branch** (`found: false` + `reason`) — the schema in
`request-contract.md`. A forced `{x,y}` invents a click for a target hidden behind a menu.
Log every call, including abstentions; a recurring abstention reason earns the next code-level
fallback.

**Free-form VQA on a grounding model needs discipline:**
- Turn thinking **off** and give real `max_tokens` headroom (512, not 64). Thinking-on with a
  small budget returns a *truncated reasoning ramble* that parses as garbage — not an error.
- Constrain the answer space in the prompt: "Reply with EXACTLY ONE word: A, B, or UNSURE",
  "Answer exactly 'yes' or 'no'". Parse with a prefix check.
- One-word enumerated state machines beat open questions for page/state detection.

**Specialist models can be prompt-brittle.** One document-OCR model degenerates into infinite
token repetition on a generic instruction and works perfectly (~166 tok/s) with the exact
grounding prompt from its model card. Use the card's prompt verbatim first, then vary.

**Prefer a real API to a VLM whenever the surface is cooperative.** An accessibility-API-first
click driver ran 2.4 s vs 6.4 s (2.7× faster), 20/20, deterministic, no GPU — the VLM became
the fallback. Pointing a 35B VLM at a screen to read data that has an HTTP endpoint is a
reflex worth catching.
