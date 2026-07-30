# Proving the local model is good enough

A local model is cheap to call and expensive to trust. This is the ladder that answers "can I
ship this?" without either hand-waving or building a research program.

## Measure the thing you consume

The most costly mistake in this file: a model was declared a failure on **MAE 8.8 vs a 7.0
threshold**, then declared a success at **6/6** on the decision the pipeline actually used.
Both numbers were correct. The pipeline consumed a *qualify/reject decision*, not a precise
score — so score error was the wrong instrument.

> Before running an eval, write down the value your code *branches on*. Measure that.
> A number that can't discriminate the difference you're gating on is a proxy, however
> rigorous it looks.

Corollary: report the confusion matrix, not just the aggregate. A model that never produced a
false negative (only over-inclusion, absorbed by a downstream human gate) is shippable at an
error rate that would look alarming as a single number.

## The golden-set gate

A small hand-labelled set (6–12 real cases), chosen **adversarially around a known failure**,
run as a script that `exit(1)`s. Assert three things at once:

1. the target case is now handled correctly,
2. **zero previously-correct cases flipped** (the regression check people forget),
3. output drift stays within a stated bound versus a **same-model baseline**.

### The baseline trap

Comparing a new prompt's output against a **stored** result from an *older or different*
model measures model-vs-model, not regression. Proof from the wild: stored score 85, the old
prompt re-run **on the current model** scored 65, the new prompt 72. Read naively, the new
prompt "regressed 13". It improved 7.

> Baseline against a **fresh same-model run of the old prompt**, computed at runtime. Anything
> else is measuring the wrong difference.

Swapping models should be one constant in the harness, so the whole gate re-runs unchanged.

## The replay harness

For anything with a store of historical decisions:

- Sample **stratified by outcome bucket**, deterministically (`ORDER BY id LIMIT n`, not
  random) so runs are comparable.
- Open the store **read-only** (`file:...?mode=ro`).
- Copy the original prompt **verbatim** so the comparison is prompt-invariant.
- Report MAE / agreement-at-threshold / confusion **and** mean model score per historical
  bucket, as a monotonicity check.
- **Carry the bucket label alongside each row**, not in a parallel list — one parse failure
  silently misaligns rows against scores otherwise.
- Run it at **concurrency 1** (see `performance.md` — batching perturbs greedy decoding and
  has already manufactured one phantom improvement).

## Calibration can make things worse

An affine fit (`1.05x − 10`) from a 30-point replay *lowered* borderline agreement from 80% →
51%. Why: the model's error was **scatter, not bias**. Subtracting a constant offset from a
noisy signal clustered right at the decision threshold flips cases in both directions.

> Fit a correction only after proving the error is *bias*. And treat any in-sample 2-parameter
> fit on ~30 points as optimistic by construction.

## Per-field, not per-schema

Reliability is a property of the **field**, not the response. In one structured output, an
`overqualified` boolean was stable across repeated identical calls while a `seniority_level`
enum flipped for the same input. A gate ANDing both leaked ~14% of the cases it was meant to
catch; gating on the stable field alone took it from 86% → 100%.

Related: **adding a field can move the other fields.** Bound it explicitly — assert score
drift ≤ *k* against the previous schema on the same model.

## Hand-read the misses

An aggregate of "86% caught" was actively misleading until each miss was read individually —
which reclassified a supposed *model capability gap* into a *gate-logic interaction*, i.e. a
bounded code fix instead of a fine-tuning project. Aggregates tell you whether to look;
they never tell you what to do.

## Audit trails as continuous eval

Append every production decision to JSONL — including **abstentions** and any free-text
`concerns`/`reason` field. This is the cheapest eval set you will ever build, and it is drawn
from the real distribution.

> A **recurring** concern in the audit earns the next named gate. Unknowns become knowns from
> data, not from guesses about what might go wrong.

## Escalation ladder — when local isn't enough

Work down this list; most problems die in the first three steps.

1. **Check config.** Is thinking on? Is enforcement live? Is SpecPrefill on? (`serving-ops.md`)
2. **Change what you measure.** Decision vs score; stable field vs noisy field.
3. **Re-tune the gate**, not the model — hand-read the misses first.
4. **Fix the prompt** (constrain the answer space, add abstention, crop the input).
5. **Retry** — for scatter, a re-run is close to an independent trial.
6. **Change model or thinking mode** — and re-certify both together, they're entangled.
7. **Route the role to a cloud model** (see `model-selection.md` for which roles may move).
8. **Fine-tune.** Last, and note the risk: narrow adapter data can *degrade* the capability the
   checkpoint already had. Keep the adapter separate and gate on the original skill being
   unchanged. In practice steps 1–6 have resolved every case so far.
