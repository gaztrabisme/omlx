# The wire contract

What oMLX actually accepts on the wire, and which fields silently do nothing.
Verified against **oMLX 0.5.3** (`GET /openapi.json` + live probes), 2026-07-29.
Re-verify after an oMLX upgrade — the surface has drifted before.

> **Standing rule (Gary, 2026-09-26): do not send generation parameters in requests.**
> Sampling, `max_tokens`, `seed` and thinking (`enable_thinking`, `thinking_budget`) are set
> per model in `~/.omlx/model_settings.json` and globally in `~/.omlx/settings.json`
> (`sampling`). Where this file measures the effect of a parameter or shows it in a payload,
> read that as what to configure on the server, not what to put in a request.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` · `/v1/completions` | the workhorse |
| POST | `/v1/embeddings` · `/v1/rerank` | rerank: **400** `"is not a reranker model"` for a served-but-wrong-type id, **404** for an id that isn't served at all |
| POST | `/v1/messages` · `/v1/messages/count_tokens` | Anthropic Messages dialect |
| POST/GET/DELETE | `/v1/responses` · `/v1/responses/{id}` | **only** place prompt-cache controls exist |
| POST | `/v1/audio/transcriptions` · `/speech` · `/process` | 404s on model lookup if no audio model installed |
| GET | `/v1/audio/voices?model=…` | `model` query param is **required** |
| GET | `/v1/models` · `/v1/models/status` | counts differ — hidden models |
| POST | `/v1/models/{id}/load` · `/unload` | `load` **blocks** until resident |
| GET/POST | `/v1/mcp/servers` · `/tools` · `/execute` | |
| GET | `/health` · `/openapi.json` | **unauthenticated** |
| GET | `/api/status` | authed; version, loaded models, tok/s, memory |
| * | `/admin/api/*` | **separate auth realm** — see `serving-ops.md` |

Auth: `Authorization: Bearer <key>` on everything under `/v1`. Missing/wrong key → `401
authentication_error`. Key lives at `auth.api_key` in `~/.omlx/settings.json`.

## `ChatCompletionRequest` — the whole field list

`model*`, `messages*`, `temperature`, `top_p`, `top_k`, `repetition_penalty`, `max_tokens`,
`stream`, `stream_options{include_usage}`, `stop[]`, `min_p`, `xtc_probability`,
`xtc_threshold`, `presence_penalty`, `frequency_penalty`, `tools[]`, `tool_choice`,
`response_format`, `structured_outputs`, `guided_grammar`, `chat_template_kwargs`,
`thinking_budget`, `specprefill`, `specprefill_keep_pct`, `specprefill_threshold`, `seed`.

**What is absent** (don't design around it): no `n`, no `logprobs`, no `logit_bias`, no
`user`, no `priority`, and **no prompt-cache key** — `prompt_cache_key`,
`prompt_cache_retention`, `service_tier`, `background`, `store`, `previous_response_id`
exist *only* on `/v1/responses`.

**`tool_choice` is half-implemented.** Measured: absent / `"auto"` / `"required"` all produce
the same behaviour (the model calls the tool if it was going to anyway); `"none"` **does**
suppress the call and returns prose. So `"required"` is **not a forcing function** — it cannot
make a model commit to a call it wasn't already making, because there is no constrained
decoding behind tool selection, only render-and-parse. If a model won't commit, the fix is the
thinking flag (measured to move invocation rates by 20+ points) or the prompt — never
`"required"`.

On a tool-call response the message has **no `content` key at all** — the raw keys are
`{"role", "tool_calls"}` — so `msg["content"]` raises `KeyError` and `msg.get("content")`
returns `None`, not `""`. Use `msg.get("content") or ""`. A clean call on the wire is
`"content" not in msg` + `finish_reason == "tool_calls"`.

### Tool calling is rendered by the model's own template — go read it

oMLX renders `tools` through the **checkpoint's shipped `chat_template.jinja`**, sitting right
there in `~/.omlx/models/<org>/<name>/`. So the wire format is a property of the *model*, not
of oMLX, and it is inspectable before you send anything:

```bash
grep -o '<|tool_call>\|<|tool>\|<|channel>thought\|<|"|>' \
     ~/.omlx/models/<org>/<name>/chat_template.jinja | sort | uniq -c
```

**No hits is a result, not a failure** — it means the model does *not* use Gemma-4's bespoke
token format, so it is almost certainly on standard JSON tool calls. Confirm with
`grep -c 'tool_call\|tools' <template>` and read the surrounding block.

**Do not assume the format is JSON.** Gemma-4's native tool syntax is a bespoke token format —
verified in the shipped template, byte-equivalent to the vendor's vLLM template:

```
declare:  <|tool>declaration:NAME{description:<|"|>...<|"|>,parameters:{...}}<tool|>
call:     <|tool_call>call:NAME{key:<|"|>value<|"|>,key2:123}<tool_call|>
```

Args are comma-separated `key:value`, **not JSON**, with strings wrapped in the `<|"|>` special
token. The template also carries a reasoning gate `<|channel>thought … <channel|>`; with
thinking off the model emits an empty thought block.

**Diagnosing "the model didn't call my tool" — separate elicitation from parsing first.** Grep
the *missed* cases' `content` for any native marker (`<|tool_call>`, `call:`, a fenced tool
block). If none appear, the model genuinely emitted prose and the parser is innocent — the
problem is elicitation, and no amount of parser work will fix it. In one 74-case audit this
came back **0/52**, which redirected the whole investigation. (The first theory there had been
"the server has no parser and emulates via forced JSON" — wrong, and expensive.)

Given an elicitation failure, in order:
1. **Turn thinking ON for the dispatch call**, with a real budget (~1.5–2k `max_tokens`).
   Measured: this recovered tested miss-cases outright. Cross-stack, the same model on the
   same think-off path went 22/100 → 54/100 invocations, so under-commitment on think-off is
   a serving-path property worth this one flag.
2. **Give it token headroom.** A truncated call leaves `<|tool_call>call` stranded in `content`
   with `finish_reason: "length"` — this reads exactly like a parse bug and is not one.
3. **Not `tool_choice: "required"`** — see above, it isn't a forcing function.

**Do not force a tool call with `response_format` or a grammar.** It was tried and rejected for
two reasons that generalize: the forced output lands in `content`, not `tool_calls`, so normal
tool plumbing never sees it; and it imposes a call-shape on *every* turn, destroying the
model's ability to decline. That is the abstention lesson above, in tool-use clothing — a
model that must always call is as useless as a schema that must always answer.

## Structured output — the #1 trap

The **only** form that enforces:

```python
payload["response_format"] = {"type": "json_schema",
                              "json_schema": {"name": name, "schema": schema}}
```

**`type` is load-bearing.** `ResponseFormat.type` defaults to `"text"`, so omitting it
**silently disables enforcement** — nothing errors, the model just talks. Independently
re-derived by at least three separate projects, and re-confirmed here by negative control on
0.5.3: same request, same model, `type` omitted → a 200-line prose ramble instead of the
object. This is the single most expensive trap on this server.

**`name` is required by the spec — send it — but it is not what enforces.** It was reported
load-bearing on oMLX 0.4.x; on **0.5.3 a negative control with `name` omitted still returned
a schema-valid object**. Treat it as mandatory anyway (the OpenAPI marks it required and the
behaviour has already changed once), but if enforcement is failing, `type` is your suspect.

**Three knobs that look interchangeable and are not:**

| Field | Wants | Enforces? |
|---|---|---|
| `response_format = {"type":"json_schema", "json_schema":{name, schema}}` | a **JSON Schema** | **yes** — the one to use |
| `response_format = {"type":"json_object"}` | — | valid-JSON-any-shape |
| `structured_outputs = {"json": schema}` | a **JSON Schema** (object or string) | **yes** on 0.5.3 — verified |
| `structured_outputs = {"grammar"/"regex"/"choice": …}` | GBNF string / regex / enum list | yes — for non-JSON constraint |
| `guided_grammar` | a **GBNF string** (folded into `structured_outputs.grammar` if absent) | yes — legacy alias |
| `guided_json` (vLLM) | — | **no — accepted and ignored** |

Verified by discriminating negative control (a schema demanding keys the model could never
invent, against a prompt about something else entirely): `response_format` and
`structured_outputs={"json": …}` both emitted the impossible keys; `guided_json` and the
no-constraint control both returned ordinary prose. No errors, no warnings, on any of them.

So there are **two working JSON paths, not one**. Prefer `response_format.json_schema` — it's
the OpenAI-compatible spelling, it's what `scripts/omlx_client.py` sends, and it's the one
with a documented degradation signal. But a vendor model card prescribing
`extra_body={"structured_outputs": {"json": schema}}` is not wrong here, despite that being
vLLM's spelling. **`guided_json` is the one that silently does nothing** — that's the vLLM
field to translate away.

### The two ways enforcement fails

The constraint engine is **xgrammar**. It fails in two distinct ways, and neither is an
ordinary error response:

1. **Soft degrade.** Compilation fails, oMLX falls back to prompt-based JSON coaxing and sets
   an HTTP **`Warning` response header** (`199 omlx "response_format not enforced;
   grammar-constrained decoding unavailable, output is best-effort"`). Status is 200; the body
   looks fine. A client that ignores that header cannot tell enforced from coaxed. Known
   trigger: **regex lookahead**, which xgrammar does not support.
2. **Hard drop.** For some malformed schemas the response stream just dies — Python raises
   `http.client.IncompleteRead`, with **no status, no body, and no Warning header**. Observed
   from a misspelled `"type"`, a schema passed as a bare string, and a **dangling `$ref`**.
   The server logs the reason; the client sees nothing. Retrying does not help.

`scripts/omlx_client.py` handles both (raises `EnforcementDegraded` / `SchemaRejected`). If
you hand-roll, read the `Warning` header *and* catch `IncompleteRead`, and when either fires
grep the server log for `grammar-constrained decoding is unavailable`.

> **Absence of a `Warning` header proves nothing about whether you asked correctly.** Both the
> `type`-omitted and `guided_json` negative controls returned unenforced prose with a clean
> 200 and no warning. The header tells you a *compilation* failed, not that your *request* was
> unconstrained. That's why the client reports what it sent — see the Output Contract.

Notes:
- `$ref` / `$defs` schemas work — but a `$ref` that doesn't resolve triggers the hard drop
  above, not a validation error.
- Strict (`additionalProperties: false` + `required` + numeric bounds) and permissive
  (`additionalProperties: true`, to pin one field and let prose ride along) are both valid
  designs — pick deliberately.
- **Keep `json.loads` + a required-key check anyway.** "Skip validation because structured
  mode is on" is a named anti-pattern; a brace-slice is the belt, key-validation is the
  braces.
- Schema enforcement works fine on `-mtp` (speculative-decoding) models, despite the general
  "spec-decode breaks constrained decoding" folklore.
- **A schema forces shape, never content, and never abstention.** See §Abstention below.

### Abstention — the deepest lesson

A schema with no "I don't know" branch **manufactures** an answer. A forced `{x:int, y:int}`
grounding schema will invent coordinates for a target that is not on screen. Give every
perception / lookup / extraction schema an explicit abstention branch:

```python
{"type": "object", "additionalProperties": False, "required": ["found"],
 "properties": {"found":  {"type": "boolean"},
                "x":      {"type": "integer", "minimum": 0, "maximum": 1000},
                "y":      {"type": "integer", "minimum": 0, "maximum": 1000},
                "reason": {"type": "string"}}}
```

…and say so in the prompt ("If it is NOT visible … respond found=false and briefly explain in
'reason'. Do NOT invent coordinates for something you cannot clearly see."). That single
change is what turns a phantom click into an honest audit line and a working fallback.

Because shape ≠ content, add **code-level fences** for anything a human will read — banned
substrings, length caps. A one-string `position_title` schema cannot stop the model writing
salary and perks *into* the string.

## Thinking control — the #2 trap

**The default is not knowable from the request — send the flag.** The *engine* default is
thinking ON, and multiple projects were burned by a model rambling and truncating before the
answer. But `enable_thinking` in `model_settings.json` overrides it per model: on one install
both large models were pinned OFF, and a negative control there showed flag-absent behaving
identically to `False`. So "on by default" is true of the engine and false of that box.
Never infer it — send it explicitly on every call, and read the per-model value when
debugging someone else's server. Prompt switches (`/no_think`, `/think`) are **ignored**.

```python
payload["chat_template_kwargs"] = {"enable_thinking": False}   # raw HTTP: top-level field
# openai SDK: extra_body={"chat_template_kwargs": {"enable_thinking": False}}
```

The cost is large and one-directional. Measured on a trivial arithmetic prompt, same model,
same temperature — `enable_thinking=False` → **3** completion tokens; `=True` → **178**
(12B) / **235** (35B) completion tokens plus 300–530 chars of `reasoning_content`, for the
identical final answer. On a model whose engine default is ON, an absent flag costs you that
~60× every call, and on a long-output task it truncates the answer instead.

- `enable_thinking` is **pure wire** — it needs no server-side flag and always works.
- `thinking_budget: N` needs **two server-side flags** in `model_settings.json`, or it is
  silently ignored: `thinking_budget_enabled: true` (else the field no-ops) and
  `reasoning_parser: "<parser>"` (else reasoning leaks inline into `content`). Both are read
  live — no restart.
- `thinking_budget` is a **soft target with no exhaustion signal**: budget 128 produced ~720
  reasoning tokens, wrapped up gracefully, `finish_reason: stop`. Never treat it as a cap.
- Setting `thinking_budget` **auto-injects `enable_thinking: true`**. A grammar-constrained
  request on a model with a `reasoning_parser` gets an auto budget so it exits thinking.
- **`max_tokens` bounds *total* generation, reasoning included** (verified 0.5.3, 2026-07-30:
  8 trials across 3 models, zero over-runs). It is a hard cap, not an advisory one.
  > An earlier note here said the opposite — "caps `content` only, does NOT bound
  > `reasoning_content`", citing a 2048-cap request running to 8,091 tokens. That was
  > observed on an earlier version; it does not reproduce on 0.5.3. **The practical advice
  > inverts:** don't set a huge `max_tokens` on the theory that it won't help anyway.
- **So the real thinking-ON failure is the opposite one: the reasoning eats the answer.**
  Measured, same prompt and model: `max_tokens=512` → 200 completion tokens, 696 chars of
  `reasoning_content`, `finish_reason: stop`, correct answer. `max_tokens=100` and `32` →
  exactly 100 and 32, `finish_reason: length`, `reasoning_content` **empty**, and the partial
  chain-of-thought leaking into `content` ("I need to calculate 17 multi…") because truncation
  beat the reasoning parser. With thinking on, budget reasoning **plus** answer — or you get a
  truncated ramble that parses as garbage rather than an error.

### The elasticity trap (measured sweep, 35B MoE)

| `thinking_budget` | reasoning chars | content chars | completion tokens |
|---|---|---|---|
| none | 1512 | 199 | 568 |
| **0** | 0 | 237 | **105** |
| 64 | 134 | **1329** | 502 |
| 256 | 503 | 1218 | 602 |
| `enable_thinking=false` | 0 | 338 | **143** |

A tight-but-nonzero budget doesn't save tokens — it **relocates the spiral into `content`**.
Want cheap? `enable_thinking=false` or budget 0. Want reasoning? Give it real room.

**Thinking on/off is task-class-specific, not a global preference.** On short single-step
tool dispatch, think-OFF was 3–15× cheaper with identical-or-better tool calls. On open-ended
generation, think-OFF produced **30% silent near-misses** (plausible output, `finish_reason:
stop`, quietly wrong) while think-ON blew past a 200 s client timeout 9/9. The catch is the
artifact gate, not the model. And note a measured case where the *model ranking itself
flipped* with the flag — model choice and dispatch-thinking config are entangled; certify
them together.

Think-ON needs output headroom: one probe truncated mid-tool-call at 500 tokens, clean at 1400.

## Sampling

Per-request params override `model_settings.json`, which overrides `settings.json`. Don't
rely on server defaults — send what you need.

- `temperature=0.0` for grounding, extraction, scoring. At temp 0 (greedy), unpublished
  `top_p`/`top_k` become inert, which is one less unknown.
- Small escalation (`0.0 → 0.2`) is a decent *retry* strategy; higher (`~0.3`) when you want
  diversity (query generation).
- **`presence_penalty` skews numeric output.** A per-model default of 1.5 measurably degraded
  a two-integer `{x,y}` coordinate response (median error 3 px → 27 px). Override it to 0 for
  numeric/structured tasks.

## Multimodal payload

```python
{"role": "user", "content": [
    {"type": "text", "text": prompt},
    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}
```

Put the **image first** when several requests share it — that's what lets the prefix cache
hit (see `performance.md`). Downscale to a sane `max_side` before encoding.

## Server-side message rewriting

oMLX preprocesses `messages` before templating, and **not identically to vLLM**. It merges
consecutive same-role turns, strips leading `<think>` / channel markers from assistant
content, and relocates system content to the front when a system message follows an assistant
turn. Consequence: a transcript-repair fix verified against another server can be a **silent
no-op** here. Verify message-shape fixes on the server you actually ship on.

## Other request bodies

- **Embeddings**: `input` (str | str[]) *or* `items[]`, `model*`, `encoding_format`,
  `dimensions` (renormalizes), `max_length`, `truncation` (default true). Server batches at
  `embedding_batch_size` (32); batching ~64 per request client-side is common practice.
- **Rerank**: `model*`, `query*`, `documents*`, `top_n`, `return_documents`,
  `max_chunks_per_doc`. Cohere/Jina-compatible.
- **STT** (multipart): `file`, `model`, `language`, `prompt` (biasing), `stream`, plus oMLX
  extensions `max_tokens` and `word_timestamps`. `response_format` and `temperature` are
  **accepted and ignored**.
- **TTS**: `model*`, `input*`, `voice`, `language`, `instructions`, `speed`, `response_format`,
  plus `ref_audio` / `ref_text` for voice cloning.
- **Anthropic `/v1/messages`** and **`/v1/responses`** both additionally accept
  `chat_template_kwargs`.
