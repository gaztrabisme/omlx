# Speed, batching, and the features that promise it

All numbers measured on Apple Silicon with 96 GB unified memory, oMLX 0.4.x–0.5.3, on the
workloads named. They are **order-of-magnitude guidance, not constants** — re-measure on
yours, warm.

## Concurrency

oMLX is a continuous-batching server (waiting queue + running set), default
`max_concurrent_requests: 8`. Match your worker pool to it — more threads just queue.

```python
with ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(fn, items))     # .map preserves order
```

**The speedup is workload-shaped. It is never 8×, and it is not one number.**

| Workload | 8 workers | Why |
|---|---|---|
| Long prompts, long structured outputs (multi-KB documents scored against multi-KB context) | **~1.4×** | prefill-bound; batching overlaps prefill but can't parallelize it linearly on one GPU |
| Mid-size structured (~450 tok in, ~100 tok out) | **~1.5×** | |
| Short prompts (~200 tok), bounded structured outputs (~160 tok) | **~2.9×** | more of the work is decode, which is what batching actually overlaps |
| Vision / any request with an image | **~1×** at prefill | images prefill individually — see below |

Both figures are measured, on the same server, minutes apart. The short-prompt number held at
2.92× idle and 2.94× with a foreign tenant holding all 8 slots — i.e. the *ratio* is robust
even when the absolute times double. **Budget 1.4–3×, never 8×**, and measure your own shape
before promising anything.

**Image requests prefill individually.** Only text-only requests batch at the prefill stage.
For vision work, concurrency helps decode and nothing else.

**Greedy is not reproducible under batching.** `temperature=0` is bit-reproducible
*sequentially*, but **not** under 8-way continuous batching: varying batch composition changes
float reduction order, which diverges tokens. Measured ~4/92 validation cases flipping between
two identical concurrency-8 runs (±1.4 pt), all stable across 3× at concurrency 1. This
produced a **phantom +4 pt improvement** in a prompt-optimization sweep that did not exist.

Independently reproduced on a 12-item structured extraction: two sequential runs were
**12/12 byte-identical**; at 4 workers 11/12, at 8 workers 10/12. And the divergence was
*structural*, not cosmetic — under batching one abstention record dropped its optional payload
fields entirely. So this is not a rounding wobble you can average away; it changes records.

**Scope the rule to what actually diverges — it is narrower than "always use 1 worker".**

| You compare | Run at | Why |
|---|---|---|
| Byte-identical output, free-text fields, or *presence* of optional fields | **workers 1** | this is what diverges, and it diverges structurally |
| A bounded/enum/boolean field you branch on | **your real worker count**, after verifying field-level stability | measured: 15/15 verdict-field agreement across w1 and w8 runs while free-text `reason` diverged |

Concurrency 1 costs **1.4–3× wall-clock** (measured 1.48× on mid-size structured work) — not
8×. The point is to pay it where it buys something. `eval-playbook.md`'s rule applies here
too: measure the value your code *branches on*. If that value is stable at 8 workers, forcing
1 buys nothing; if you're diffing records or reading free text, forcing 1 is the whole game.

## Caching — the biggest lever you already have

Prefix cache (+ an SSD cold tier) reuses KV across shared prompt prefixes. Measured **49 s →
1.7 s at 8K tokens** on a hit. A lifetime prefix-cache hit rate around 38% is normal for mixed
traffic.

To actually hit it:
- **Put the invariant part of the prompt first** — system prompt, then shared context, then
  the per-item variable part.
- **Image first** in multimodal messages when a batch shares the image.
- Don't shuffle system-prompt wording between calls that should share a prefix.
- `/admin/api/cache/probe` predicts a hit for a given message set, if you need to verify.

Note the in-memory hot cache may be disabled (`hot_cache_max_size: 0`) with everything going
through the SSD tier — check, it changes the latency profile.

## The four acceleration features — verdicts

| Feature | What it is | Verdict |
|---|---|---|
| **Prefix / SSD cache** | KV reuse across shared prefixes | **ON. The #1 lever.** Free, no quality cost. |
| **MTP** (`-mtp` models) | native multi-token prediction, lossless | **ON when available.** ~1.1×, but see the alignment tax. |
| **TurboQuant KV** | KV-cache quantization, 2–8 bits | Off by default. Batch-aware; payoff only at long context / high concurrency. Enable deliberately, measure. |
| **SpecPrefill** | draft model scores token importance, prefills only the top `keep_pct` | **Off, and dangerous.** Lossy by design. |
| **DFlash** | block-diffusion speculative decode, 3–4× | **Off.** Single-stream only; evicts the batched engine. |

### MTP — real, small, and silently optional

Measured on a 35B MoE, structured 50-item JSON, 300 tokens, greedy: base **55.0 → 60.5 tok/s
(~1.1×)**, acceptance **94.8%**, output **byte-identical** (speculative decoding verifies every
drafted token, so it is lossless by construction).

The catches:
- **MTP engages only at batch 1, or for batches whose cache positions are aligned.** Under
  staggered concurrent arrivals it silently falls back. A naive tok/s number under load will
  look fine while MTP does nothing. Don't re-litigate this by benchmarking a bundle — the
  fallback is automatic and invisible.
- Acceptance depends on output length: 94.8% on long structured text, only **33–60%** on
  6–18-token grounding replies. For short outputs, **prefill dominates latency anyway**.
- "Greedy maximizes acceptance" is **false** as a general rule — at temp > 0 oMLX uses
  probabilistic rejection sampling (`min(1, p_t/p_d)`), which is *more* lenient than greedy's
  argmax match; acceptance climbed above 90% on a sampled preset.
- The MTP head is **baked into the target checkpoint's shards** — `mtp_enabled` is a bare
  boolean with no draft-model companion. You cannot point checkpoint A's head at checkpoint B.
  (VLM MTP is the exception: it *does* take an external drafter.)
- oMLX **consumes** MTP heads; it does not train them. Only some architecture families have a
  patched forward path — check before assuming.

### SpecPrefill — the one that poisons results

SpecPrefill drops context tokens above a threshold, keeping only the top `keep_pct`. Enabled
server-side mid-run, it caused a document-extraction backfill to **extract from ~20% of each
document's tokens**; 75 extractions had to be deleted and redone. The only symptom was a log
line: `SpecPrefill: sparse prefill N/M conv tokens`.

> Fine for retrieval-style chat where you just need the gist. **Poison for coverage tasks** —
> extraction, summarization-for-completeness, audit, anything where "did you read all of it"
> is the point. And note **the client cannot see the setting per request**; it is a
> server-side decision your code cannot detect. Check config; grep the log.

## Context budget

KV cache is ~**80 KB/token** for a 40-layer / 2-KV-head / 256-head-dim model at fp16
(≈2.5 GB @ 32K, 10 GB @ 128K, 20 GB @ 256K; roughly ÷3–4 with 4-bit TurboQuant).

Three ceilings, and the binding one is not the obvious one:

| Ceiling | Binds at |
|---|---|
| Memory | ~500K tokens — **not binding** |
| Latency (interactive) | ~16–32K |
| **Quality / reasoning** | **~32K comfortable, ~64K wall — this is the real limit** |

> "256K-native" is the **wrong number to size against.** Size against what the model still
> reasons well over, then verify with your own eval. A previously-believed hard 32K server cap
> turned out to be a phantom — but the working number stayed ~the same for an entirely
> different reason. Keep the value, replace the rationale.

## Benchmarking discipline

**Always warm both sides.** A famous "oMLX is 20× slower than in-process" result (432 s vs
20 s) was a warm-vs-cold comparison: oMLX JIT-compiles custom attention kernels on the
first-ever call (~7 min), then caches them to disk. Warm-vs-warm, the two were within noise
(35.4 vs 34 tok/s). This was theorized wrong twice before one warm-vs-warm benchmark settled
it — gate on the artifact, not the theory.

**Probe before a long run.** Sustained decode on a 300-token completion is the cheap canary —
`scripts/omlx_probe.sh --model <id> --canary` runs exactly this and fails below a healthy
floor. If a normally ~70 tok/s model reports 11–16 tok/s, something is wrong. Causes in the
order you should check them:

1. **Another tenant is holding scheduler slots.** `active_requests` at capacity means your
   requests queue and every timing you take is confounded. This is the most common cause and
   the easiest to miss — an 8-slot server showing `active=8` looks like a healthy busy server.
   (`omlx_probe.sh` now fails on this rather than printing it as trivia.)
2. **The engine pool is loading or evicting another model**, starving the resident one.
3. **Memory-guard prefill pauses** — stalls, not errors.

Under ~20 tok/s single-stream: investigate. Don't assume failure, and don't just wait.

## Reference throughput

| Workload | Observed |
|---|---|
| 35B MoE oQ8, single stream, warm decode | ~55–70 tok/s (85–92 with a warm VLM path) |
| same, `-mtp`, single stream, greedy | ~60 tok/s (~1.1×) |
| 35B MoE under ~8-way batching | **1.4–7 tok/s per request** — this is per-request under load, never quote it as headline speed |
| 12B 4-bit, warm decode | ~35 tok/s (matches the same model run in-process) |
| Specialist 8-bit OCR model, warm | ~166 tok/s |
| Prefill | ~90 tok/s average across mixed traffic |
| Model reload from disk (small model) | ~7 s |
| Cold load, ~38 GB model | 9.8–17 s (0.26–0.44 s/GB) — varies with cache state; time your own |
| Prefix-cache hit @ 8K | 49 s → 1.7 s |

Server-wide `avg_generation_tps` in `/api/status` is a *lifetime, load-mixed* number. It is
not your model's speed.
