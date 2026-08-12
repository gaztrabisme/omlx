# Operating the box

Everything between "the server exists" and "the server answers correctly".
Values below are from a live install of **oMLX 0.5.3** on an Apple Silicon laptop with 96 GB
unified memory, read 2026-07-29. **Read your own config; do not trust these numbers.**

## Config-first — the gate

`~/.omlx/settings.json` (global) and `~/.omlx/model_settings.json` (per model) decide whether
half the documented request fields do anything at all. Read them **before** probing.

A recorded trace: black-box experimentation against these flags produced "a confident, wrong,
fabricated-causality conclusion" that took a second investigation to unwind. Config first,
then probe to confirm.

The fields that change behaviour silently:

| File | Key | Why it bites |
|---|---|---|
| `model_settings.json` | `enable_thinking` | pinned per model; overrides the engine default |
| | `thinking_budget_enabled` | **false ⇒ `thinking_budget` in your request is ignored** |
| | `reasoning_parser` | unset ⇒ reasoning leaks inline into `content` |
| | `specprefill_enabled` | true ⇒ prompts are **sparsely** prefilled — see `performance.md` |
| | `turboquant_kv_enabled` | KV-cache quantization; **off** by default despite folklore |
| | `mtp_enabled` / `vlm_mtp_enabled` | speculative decoding, silent fallback |
| | `is_pinned` / `is_default` / `is_hidden` | pinning is how you stop LRU eviction |
| | `forced_ct_kwargs` | keys here are **unoverridable** from the request |
| `settings.json` | `scheduler.max_concurrent_requests` | match your worker pool to it |
| | `sampling.max_context_window` | a **fallback**, not a cap — see below |
| | `memory.*` guard tier + thresholds | prefill pauses near the ceiling |
| | `idle_timeout` (seconds) | unpinned models unload after this |
| | `auth.api_key` / `skip_api_key_verification` | where the key lives |

Both files are read live for most keys — no restart needed for the thinking flags.

### `max_context_window` is not a cap

It is a three-tier resolver: (1) a per-model override, never clamped; (2) the model's
config-discovered **native** context, clamped only if `max_context_window_policy` is set;
(3) `sampling.max_context_window` as a fallback, reached only when 1 and 2 both fail. With no
per-model override and a null policy, **the effective window is the model's native value**,
and the settings number is dead code. Confirmed live via `/v1/models` per-model
`max_model_len`. Don't size against the settings value, and don't size against "native"
either — see the context-budget section of `performance.md`.

## The CLI

```
omlx {start,stop,restart,serve,launch,diagnose} [--version]
```

- `start` / `stop` / `restart` drive the GUI app's managed background server.
- `serve` runs a foreground server: `--model-dir --host --port --log-level
  {trace,debug,info,warning,error} --max-concurrent-requests --embedding-batch-size
  --memory-guard {safe,balanced,aggressive} --memory-guard-gb --paged-ssd-cache-dir
  --paged-ssd-cache-max-size --hot-cache-max-size --no-cache --initial-cache-blocks
  --mcp-config --hf-endpoint --hf-cache/--no-hf-cache --base-path --api-key` (plus proxy/CA
  flags). `--log-level trace` logs full message content — useful once, noisy always.
- `launch <tool>` wires an external agent CLI at the local server (`list` shows the roster).
- **`diagnose` is not a health check.** It takes one literal subcommand for a menubar
  visibility check. For health, use `/health` + `/api/status`, or `scripts/omlx_probe.sh`.

## Lifecycle traps

**`GET /v1/models` is not a health check, and using it as one is the most common
substitution for the probe.** It enumerates what is *configured*, not what is *loaded*, so it
answers 200 with a full model list against a server holding nothing in memory. Measured
2026-08-12, same instant:

```bash
curl -s -H "Authorization: Bearer $K" :8000/v1/models    | jq '.data | length'   # 7
curl -s -H "Authorization: Bearer $K" :8000/api/status   | jq '.models_loaded'   # 0
```

Seven models listed, zero resident. Every caller that took the first line as "oMLX is up"
green-lit a cold load of tens of GB as if it were a warm call — the cost the probe exists to
surface. **`models_loaded` / `loaded_models` from `/api/status` is the truth**; `/v1/models`
tells you only that the process is answering.

This is not hypothetical drift: `omlx_probe.sh` has not run since 2026-07-30, and in the same
window ad-hoc `curl /v1/models` health checks appear across four separate projects. One of
them documents the probe as a prerequisite in its own `CLAUDE.md` and then doesn't run it.
The probe is one command and it distinguishes the two states; a curl that cannot tell them
apart is worse than no check, because it returns green.

**Don't `omlx restart` while the GUI app owns the server.** The app respawns its own server
and you get a collision.

**The zombie-port race.** A Metal fault wedged one endpoint (`/v1/embeddings` returning 500)
while chat kept working, because the engine pool had *cached the load failure*. Killing the
GUI restarted only the wrapper — the real server process survived, and the relaunched one
**raced it for port 8000**. Symptom: `curl` returns 200 while the SDK path returns 500,
nondeterministically. Fix:

```bash
lsof -nP -ti :8000        # find the REAL listener
kill <pid>                # then relaunch, and verify through the SDK path, not curl
```

**Cold start is expensive and silent.** Load cost is roughly linear in weights, but the
rate varies a lot with cache state: **0.26–0.9 s/GB** measured across runs (a ~38 GB model
took 9.8 s, 11.5 s and ~17 s on different occasions). Time your own `POST /v1/models/{id}/load`
rather than trusting a rate. The first-ever call on a fresh install is worse: oMLX
JIT-compiles its own attention kernels (~7 min once), then caches to disk. Budget for it, or
warm explicitly with `POST /v1/models/{id}/load` (which blocks).

**Port 8000 is contested, and oMLX wins quietly.** The zombie-port race above is oMLX
against *itself*. These two are oMLX against *someone else's service*, and both bit inside
eleven days:

- **oMLX absorbs a neighbour's port.** It came up mid-run on `127.0.0.1:8000`, the port
  another service had published, and swallowed all loopback traffic to it. The tell that
  wasted the time: **oMLX's FastAPI answers `/openapi.json`**, so the neighbour's health
  check kept passing while its uploads 404'd. Fix was to republish the neighbour elsewhere.
- **The IPv4/IPv6 split.** `python3 -m http.server 8000` bound `*:8000` on **IPv6** without
  erroring, because oMLX holds **IPv4** `127.0.0.1:8000`. An agent's `curl localhost:8000`
  resolved `::1` and reached the new server; a browser resolved IPv4 and reached oMLX's
  `{"detail":"Not Found"}`. Both parties reported that it "worked", on different servers.

So `lsof -nP -ti :8000` is not enough — it will happily show one listener per family. Check
both, and treat a 200 from port 8000 as "something answered", never as "my service answered":

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN        # every listener, both families
curl -s :8000/openapi.json | head -c 80 # if this is oMLX's schema, it is oMLX
```

**Unpinned models vanish.** `idle_timeout` (900 s here) unloads them. That is a fine way to
retire a model without a restart, and a nasty surprise mid-pipeline. Pin what must stay hot.

**oMLX won't always auto-evict.** With ~51 GB of models under a ~78 GB ceiling nothing gets
evicted on its own; if you need the memory, unload explicitly. Phased load/unload beats
hoping LRU does the right thing.

## Model management

- Models live at `~/.omlx/models/<org>/<name>/` and are auto-discovered by the presence of
  `config.json` + `*.safetensors`.
- **Use real directories, not symlinks.** An external symlink under the model dir froze
  startup entirely (HTTP 000 on every endpoint). Copy or move.
- With `hf_cache_enabled`, models in the HuggingFace cache are also discoverable, under
  **`org--name`** ids (double hyphen). Get the id from `/v1/models`, never guess it.
- `/v1/models` may include non-LLM pseudo-models (e.g. a document-conversion shim exposed as
  a model). Don't send chat to them.
- `/v1/models` and `/v1/models/status` can report different counts — the delta is hidden
  models (`is_hidden: true`). Helper checkpoints are auto-hidden by naming convention
  (`_assistant` / `_mtp` suffixes, draft-config blocks).
- **Not every VLM architecture is servable.** Some fail with `VLM loading failed … Missing N
  parameters`; the Qwen-VL family is the well-supported lane. If oMLX can't serve it, the
  fallback is running the vision library in-process.

### Reading a model id

Quantization suffixes are meaningful. `<base>-oQ<level>[e][-fp16][-mtp]`:

- **oQ** = oMLX's mixed-precision weight quantization; levels `2, 2.5, 2.7, 3, 3.5, 4, 5, 6, 8`
  (fractional levels add routed-expert protection). `e` = enhanced. Default group size 64.
- **`-fp16`** = quantized in float16 rather than the default bfloat16.
- **`-mtp`** = the multi-token-prediction / nextn head was **preserved** through quantization
  → native speculative decoding is available (see `performance.md`).
- **OptiQ** is a *different*, third-party mixed-precision scheme — not oQ.
- **TurboQuant** is **KV-cache** quantization (bits `2, 2.5, 3, 3.5, 4, 6, 8`), unrelated to
  the weight quant in the model name.

## The admin realm

`/admin/api/*` is a **separate auth realm** — the `/v1` Bearer key is rejected with
`Admin authentication required`. Log in for a cookie session:

```bash
curl -c jar -X POST http://127.0.0.1:8000/admin/api/login \
     -H 'Content-Type: application/json' -d '{"api_key":"'"$OMLX_API_KEY"'"}'
curl -b jar http://127.0.0.1:8000/admin/api/models
```

One `requests.Session` can carry the Bearer header for `/v1` and the cookie for `/admin`.

What lives there (80+ endpoints): global + per-model settings (`ModelSettingsRequest` is the
authoritative list of ~50 tunables), model profiles and templates, stats/activity/logs,
`cache/probe` (prefix-cache hit prediction), hot/SSD cache clear, HuggingFace search/download,
oQ quantization jobs, HF upload, a benchmark + accuracy-eval queue, grammar parsers, sub-keys.

**A settings-signature change forces an engine unload+reload.** So exposed "profiles" are a
*mode switch per workload*, not per-request routing — interleaving two profiles thrashes a
multi-GB reload.

## Memory

- Ceiling is oMLX's static limit (~78 GB here) *versus* the OS Metal cap. If
  `iogpu.wired_limit_mb` is unset, macOS's default `max_recommended_working_set_size` may sit
  **below** oMLX's ceiling and you silently lose headroom. The server logs this once per start.
  Raising it needs `sudo` and is **non-persistent across reboot**:
  ```bash
  sudo sysctl iogpu.wired_limit_mb=<megabytes>
  ```
  This is a machine-owner decision — surface it, don't run it unasked.
**There are two memory guards, and they behave oppositely.** Do not generalize one to the other.

1. **Prefill guard — stalls, silently.** (`balanced` tier, soft 0.85 / hard 0.95.) It *pauses
   prefill* near the ceiling rather than failing. Symptom is a stall, not an error. Huge
   concurrent prefills are the usual cause: ~100k-char raw HTML documents × 8 workers produced
   23-minute average calls and 1–2 hour stalls. **Clean and bound your inputs at ingest.**
2. **Model-load admission guard — refuses loudly, with HTTP 507.** Immediate, deterministic
   (51/51 in one log), and the body carries the whole diagnosis:
   ```
   507: Cannot load <model>: projected memory 77.55GB would exceed the memory ceiling
   70.72GB (current: 39.80GB, model: 37.75GB). Free system memory or lower memory_guard_tier.
   ```
   Retrying is pointless and re-pressures a box already out of memory. Unload something
   (`POST /v1/models/{id}/unload`) or lower the tier. `scripts/omlx_client.py` raises
   `ServerRefused` carrying that message; a naive client shows only
   `HTTP Error 507: Insufficient Storage` and throws the actionable half away.

**The ceiling is not a constant.** Observed in one session: 70.25 / 70.71 / 70.72 / 70.91 /
71.13 GB in the log, while `/api/status` reported 78.5 GB and 83.5 GB at different moments. It
moves with what else the machine is doing. Size with real headroom; never compute "will it
fit" from a remembered number.
- MLX allocator bloat can pin usage high; a model swap has been observed to reset it.

## Logs

`~/.omlx/logs/server.log` (+ dated rotations, short retention). Worth grepping:

| Pattern | Means |
|---|---|
| `SpecPrefill: sparse prefill N/M` | **you are only seeing part of each prompt** |
| `grammar-constrained decoding is unavailable` | **your schema didn't compile** — output is coaxed, or the connection died. The server-side twin of the `Warning` header, and the only signal when the connection drops. |
| `Model '<id>' not found` | client is using a stale or wrong model id |
| `Metal cap … below the oMLX static ceiling` | `iogpu.wired_limit_mb` unset |
| `MTP path activated for uid=…` | speculative decoding actually engaged |
| `Using boundary cache snapshot for …` | prefix cache hit |
| `compiled … path failed … falling back to eager` | a silent per-model perf regression |
| `Chat completion: N tokens in Ts (X tok/s)` | your real throughput series |

A clean log is normal: memory-guard init, cache-phase timings, and rotating-KV-cache chatter
are all benign.

## Note on patching

oMLX ships as a signed macOS app with its own bundled Python. The source is readable inside
the bundle (useful for answering "what does this field actually do"), but **editing it breaks
the signature and is lost on update**. Treat oMLX as a black box you configure, not a library
you patch — that constraint shapes the whole option space.
