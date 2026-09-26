#!/usr/bin/env bash
# oMLX pre-flight gate — run this before spending an hour on a run.
#
# Verified against oMLX 0.5.3 on 2026-07-29.
#
# Answers, in order: is the server up, does my key work, is my model actually LOADED (or am
# I about to eat a cold load), and which server-side flags will silently change my results?
#
#   ./omlx_probe.sh                        # health + config audit
#   ./omlx_probe.sh --model <id>           # also: is THAT model loaded, and cold-load cost
#   ./omlx_probe.sh --model <id> --deep    # also: PROVE schema enforcement is live (1 request)
#   ./omlx_probe.sh --model <id> --canary  # also: sustained-decode tok/s (is the box healthy?)
#
# Exits non-zero if the server is down, the key is rejected, --deep enforcement fails, or
# the scheduler is saturated by other traffic (your run would be queued behind it).
# Config: OMLX_ENDPOINT (default http://127.0.0.1:8000/v1), OMLX_API_KEY, OMLX_BASE_PATH.
set -uo pipefail

ENDPOINT="${OMLX_ENDPOINT:-http://127.0.0.1:8000/v1}"
ENDPOINT="${ENDPOINT%/}"
ROOT="${ENDPOINT%/v1}"
BASE="${OMLX_BASE_PATH:-$HOME/.omlx}"
MODEL=""; DEEP=0; CANARY=0; RC=0

while [ $# -gt 0 ]; do
  case "$1" in
    --model)  MODEL="${2:-}"; shift 2 ;;
    --deep)   DEEP=1; shift ;;
    --canary) CANARY=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Key: env first, then the server's own settings file. No literal key in this repo.
KEY="${OMLX_API_KEY:-}"
if [ -z "$KEY" ] && [ -r "$BASE/settings.json" ]; then
  KEY="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["auth"]["api_key"])' \
         "$BASE/settings.json" 2>/dev/null)"
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf 'ok   %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; RC=1; }

say "== oMLX probe =="
say "endpoint: $ENDPOINT"

# ---------------------------------------------------------------- 1. liveness
if ! curl -sf --max-time 5 "$ROOT/health" >/dev/null 2>&1; then
  bad "server not answering at $ROOT/health"
  say "     start it (GUI app, or: omlx serve --base-path $BASE), then re-run."
  say "     if it 'should' be up: lsof -nP -ti :${ENDPOINT##*:} — a zombie listener can hold the port."
  exit 1
fi
ok "server is up"

# ---------------------------------------------------------------- 2. auth
if [ -z "$KEY" ]; then
  bad "no API key (set OMLX_API_KEY, or make $BASE/settings.json readable)"
  exit 1
fi
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        -H "Authorization: Bearer $KEY" "$ENDPOINT/models")"
case "$CODE" in
  200) ok "api key accepted" ;;
  401) bad "api key REJECTED (401) — wrong key, or it changed in settings.json"; exit 1 ;;
  *)   bad "unexpected $CODE from $ENDPOINT/models"; exit 1 ;;
esac

# ---------------------------------------------------------------- 3. status
STATUS="$(curl -s --max-time 10 -H "Authorization: Bearer $KEY" "$ROOT/api/status")"
python3 - "$STATUS" "$BASE" <<'PY'
import json, sys
from pathlib import Path
try:
    s = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
loaded = s.get("loaded_models") or []
print(f"     version {s.get('version','?')} | discovered {s.get('models_discovered','?')}"
      f" | loaded {loaded or 'none'}")
print(f"     default model: {s.get('default_model','?')}")
mem, cap = s.get("model_memory_used"), s.get("model_memory_max")
def gb(v):
    try: return f"{float(v) / 1e9:.1f} GB"
    except (TypeError, ValueError): return str(v)
if mem: print(f"     model memory: {gb(mem)} / {gb(cap)}")

# Saturation: a foreign tenant holding every slot means YOUR requests queue behind it, and
# every timing you take is confounded. This is not an info line — it is a stop sign.
act, wait = s.get("active_requests"), s.get("waiting_requests")
cap_n = None
try:
    cap_n = json.loads((Path(sys.argv[2]) / "settings.json").read_text()) \
            ["scheduler"]["max_concurrent_requests"]
except Exception:
    pass
if act is not None:
    print(f"     in flight: active={act} waiting={wait} (capacity {cap_n})")
    if cap_n and act >= cap_n:
        print(f"FAIL scheduler SATURATED — {act}/{cap_n} slots held by other traffic.")
        print( "     Your requests will queue; any throughput you measure now is confounded.")
        print( "     Wait for it to drain, or accept that timings are meaningless.")
        raise SystemExit(4)
    if cap_n and act >= max(1, cap_n // 2):
        print(f"WARN scheduler {act}/{cap_n} busy — timings will be noisy.")
PY
if [ $? -eq 4 ]; then
  RC=1
  # A saturated scheduler makes --canary and --deep meaningless (and --canary will just eat
  # the queue and report a 507 or a fake-slow number). Stop here rather than produce numbers
  # that read as measurements.
  if [ "$CANARY" = "1" ] || [ "$DEEP" = "1" ]; then
    say ""
    say "== probe FAILED — skipping --canary/--deep: results would be confounded =="
    exit 1
  fi
fi

# ---------------------------------------------------------------- 4. the model you named
if [ -n "$MODEL" ]; then
  python3 - "$STATUS" "$MODEL" "$ENDPOINT" "$KEY" <<'PY'
import json, sys, urllib.request
status_raw, model, endpoint, key = sys.argv[1:5]
try: status = json.loads(status_raw)
except Exception: status = {}
req = urllib.request.Request(f"{endpoint}/models", headers={"Authorization": f"Bearer {key}"})
try:
    ids = {m["id"]: m for m in json.load(urllib.request.urlopen(req, timeout=15))["data"]}
except Exception as e:
    print(f"WARN could not list models: {e}"); sys.exit(0)
if model not in ids:
    print(f"FAIL model {model!r} is NOT served. Served ids:")
    for i in sorted(ids): print(f"       {i}")
    print("       (HF-cache models use org--name, double hyphen. Never guess the id.)")
    sys.exit(3)
ctx = ids[model].get("max_model_len")
if ctx is None:
    print(f"WARN {model} reports no context length — this is very likely a NON-LLM shim "
          "(e.g. a document-conversion pseudo-model) exposed in /v1/models. Do not send chat "
          "to it.")
else:
    print(f"ok   model {model} is served (native ctx: {ctx})")
if model in (status.get("loaded_models") or []):
    print("ok   model is LOADED — no cold start")
else:
    print(f"WARN model is NOT loaded — first call pays a cold load "
          f"(~0.9 s/GB; a ~36 GB model is ~35 s).")
    print(f"     warm it explicitly: curl -X POST -H 'Authorization: Bearer <key>' "
          f"{endpoint}/models/{model}/load")
    print( "     loading it may also evict whatever is resident now.")
PY
  [ $? -eq 3 ] && RC=1
fi

# ---------------------------------------------------------------- 5. silent-behaviour flags
say ""
say "-- config that silently changes results --"
python3 - "$BASE" "$MODEL" <<'PY'
import json, sys
from pathlib import Path
base, model = Path(sys.argv[1]), sys.argv[2]

g = base / "settings.json"
if g.exists():
    s = json.loads(g.read_text())
    sch, smp, mem = s.get("scheduler", {}), s.get("sampling", {}), s.get("memory", {})
    print(f"     max_concurrent_requests : {sch.get('max_concurrent_requests')}"
          "   <- match your worker pool to this")
    print(f"     chunked_prefill         : {sch.get('chunked_prefill')}")
    idle = s.get("idle_timeout")
    if isinstance(idle, dict):
        idle = idle.get("idle_timeout_seconds")
    print(f"     idle_timeout (s)        : {idle}"
          "   <- unpinned models unload after this")
    print(f"     memory guard            : {mem.get('memory_guard_tier')}"
          f" (soft {mem.get('soft_threshold')} / hard {mem.get('hard_threshold')})"
          "   <- stalls prefill, does not error")
    print(f"     sampling.max_context_window : {smp.get('max_context_window')}"
          f"  policy={smp.get('max_context_window_policy')}")
    print( "         ^ a FALLBACK, not a cap. Effective window = the model's native ctx above.")
    if not s.get("auth", {}).get("skip_api_key_verification", True):
        print("     auth                    : key verification ON")
else:
    print(f"WARN no {g} — cannot audit global config")

m = base / "model_settings.json"
if not m.exists():
    print(f"WARN no {m} — cannot audit per-model flags"); raise SystemExit(0)
ms = json.loads(m.read_text())
ms = ms.get("models", ms)          # 0.5.x wraps the map under "models"
if model:
    # Never silently substitute another model's config for the one that was asked about.
    if model not in ms:
        print(f"     [{model}]")
        print( "       WARN no per-model settings entry — engine defaults apply, and they are")
        print( "            not visible here. Add an entry in model_settings.json; do not")
        print( "            send sampling or thinking in requests.")
        raise SystemExit(0)
    names = [model]
else:
    names = sorted(ms)
RISK = [
    ("specprefill_enabled",    True,  "SPARSE PREFILL — the model sees only part of each long "
                                      "prompt. Poison for extraction/coverage tasks."),
    ("dflash_enabled",         True,  "DFlash on — single-stream only; it evicts the batched "
                                      "engine, killing concurrency."),
    ("thinking_budget_enabled", False, "thinking_budget in your request will be SILENTLY IGNORED."),
]
for n in names:
    cfg = ms.get(n, {})
    print(f"\n     [{n}]")
    for key, trigger, msg in RISK:
        val = cfg.get(key)
        if val == trigger:
            print(f"       WARN {key}={val}: {msg}")
    et = cfg.get("enable_thinking")
    note = "  <- ON: costs ~60x tokens and can truncate the answer" if et is True else \
           "  <- unset: engine default (ON) applies" if et is None else ""
    print(f"       enable_thinking default : {et}{note}")
    print( "                                 (send the flag explicitly; never rely on this)")
    if not cfg.get("reasoning_parser"):
        print("       note reasoning_parser unset (some models have a native reasoning "
              "template; if not, thinking output leaks into content).")
    for key in ("turboquant_kv_enabled", "mtp_enabled", "vlm_mtp_enabled", "is_pinned"):
        if key in cfg:
            print(f"       {key}: {cfg[key]}")
    for key in ("temperature", "top_p", "top_k", "presence_penalty"):
        if key in cfg:
            extra = "  <- skews numeric/coordinate output; override to 0" \
                    if key == "presence_penalty" and cfg[key] else ""
            print(f"       default {key}: {cfg[key]}{extra}")
PY

# ---------------------------------------------------------------- 6. wired-memory ceiling
if command -v sysctl >/dev/null 2>&1; then
  WIRED="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo "")"
  if [ "${WIRED:-0}" = "0" ] || [ -z "$WIRED" ]; then
    warn "iogpu.wired_limit_mb is unset — macOS's default Metal cap may sit BELOW oMLX's"
    say  "     ceiling, silently costing long-context/high-concurrency headroom."
    say  "     Machine owner can raise it (needs sudo, NOT persistent across reboot):"
    say  "       sudo sysctl iogpu.wired_limit_mb=<megabytes>"
  else
    ok "iogpu.wired_limit_mb = $WIRED"
  fi
fi

# ---------------------------------------------------------------- 7. --canary: is it healthy?
if [ "$CANARY" = "1" ]; then
  say ""
  say "-- canary: sustained decode (the cheap 'is this box healthy' check) --"
  if [ -z "$MODEL" ]; then
    bad "--canary needs --model <id>"
  else
    python3 - "$ENDPOINT" "$KEY" "$MODEL" <<'PY' || RC=1
import json, sys, time, urllib.request
endpoint, key, model = sys.argv[1:4]
hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}

# Warm the model FIRST. Timing a cold load and calling the result "warm decode" is the
# warm-vs-cold benchmarking error this skill bans — and it produced a 4.4x false alarm
# (13.9 tok/s cold vs 60.9 tok/s warm, same command 60s apart).
root = endpoint.rsplit("/v1", 1)[0]
try:
    st = json.load(urllib.request.urlopen(
        urllib.request.Request(root + "/api/status", headers=hdr), timeout=10))
    loaded = st.get("loaded_models") or []
except Exception:
    loaded = []
if model not in loaded:
    print(f"     {model} not resident — loading before timing (this is NOT the canary)...")
    t0 = time.time()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{endpoint}/models/{model}/load", data=b"{}", headers=hdr), timeout=900)
        print(f"     cold load took {time.time() - t0:.1f}s")
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try: detail = json.loads(e.read()).get("error", {}).get("message", "")[:200]
            except Exception: pass
        print(f"FAIL could not load {model}: {type(e).__name__} {detail}")
        if "memory ceiling" in detail:
            print("     ^ memory-guard ADMISSION refusal (HTTP 507). Unload another model or")
            print("       lower memory_guard_tier. Retrying will not help.")
        raise SystemExit(1)

p = {"model": model,
     "messages": [{"role": "user", "content":
                   "Count from 1 to 120, one number per line, nothing else."}]}
r = urllib.request.Request(endpoint + "/chat/completions", data=json.dumps(p).encode(),
                           headers=hdr)
t0 = time.time()
try:
    with urllib.request.urlopen(r, timeout=300) as resp: b = json.load(resp)
except Exception as e:
    print(f"FAIL canary request failed: {type(e).__name__}: {e}"); raise SystemExit(1)
dt = time.time() - t0
tok = b.get("usage", {}).get("completion_tokens") or 0
tps = tok / dt if dt else 0
print(f"     {tok} tokens in {dt:.1f}s = {tps:.1f} tok/s (single stream, warm)")
# Deliberately loose floors: these separate "healthy" from "something is wrong", not
# model from model. Re-derive them for your own hardware.
FLOOR = 8.0
if tps < FLOOR:
    print(f"FAIL {tps:.1f} tok/s is far below any healthy single-stream rate (<{FLOOR}).")
    print( "     Likely causes, in order: (1) other traffic is holding scheduler slots —")
    print( "     check `active_requests` above; (2) the engine pool is loading/evicting")
    print( "     another model; (3) memory-guard prefill pauses. Do NOT start a long run.")
    raise SystemExit(1)
if tps < 20:
    print(f"WARN {tps:.1f} tok/s is low for a warm single stream — investigate before")
    print( "     trusting any timing you take now.")
else:
    print("ok   sustained decode looks healthy")
PY
  fi
fi

# ---------------------------------------------------------------- 8. --deep: prove enforcement
if [ "$DEEP" = "1" ]; then
  say ""
  say "-- deep check: is JSON-schema enforcement actually live? --"
  if [ -z "$MODEL" ]; then
    bad "--deep needs --model <id>"
  else
    HDR="$(mktemp)"; BODY="$(mktemp)"
    curl -s -D "$HDR" -o "$BODY" --max-time 180 \
      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
      -X POST "$ENDPOINT/chat/completions" -d @- <<JSON
{"model": "$MODEL",
 "response_format": {"type": "json_schema", "json_schema": {"name": "probe",
   "schema": {"type": "object", "additionalProperties": false, "required": ["ok", "n"],
              "properties": {"ok": {"type": "boolean"},
                             "n": {"type": "integer", "minimum": 1, "maximum": 3}}}}},
 "messages": [{"role": "user", "content": "Reply with ok=true and n=2."}]}
JSON
    if grep -qi '^warning:' "$HDR"; then
      bad "server set a Warning header — it DEGRADED to prompt coaxing; output is NOT enforced"
      grep -i '^warning:' "$HDR" | sed 's/^/     /'
    else
      ok "no Warning header (grammar-constrained decoding was used)"
    fi
    python3 - "$BODY" <<'PY' || RC=1
import json, sys
body = json.load(open(sys.argv[1]))
if "error" in body:
    # Surface the server's own message — it is the actionable part, and hiding it behind a
    # KeyError is the same mistake the client's ServerRefused exists to prevent.
    print(f"FAIL server rejected the probe: {body['error'].get('message')}")
    raise SystemExit(1)
try:
    content = body["choices"][0]["message"]["content"]
except Exception as e:
    print(f"FAIL unexpected response shape ({e}): {json.dumps(body)[:200]}")
    raise SystemExit(1)
try:
    obj = json.loads(content)
except Exception:
    print(f"FAIL response is not JSON — enforcement is NOT live: {content[:200]!r}")
    raise SystemExit(1)
missing = [k for k in ("ok", "n") if k not in obj]
if missing:
    print(f"FAIL schema keys missing {missing}: {obj}"); raise SystemExit(1)
print(f"ok   schema-valid object returned: {obj}")
PY
    rm -f "$HDR" "$BODY"
  fi
fi

say ""
[ "$RC" = "0" ] && say "== probe PASSED ==" || say "== probe FAILED — see FAIL lines above =="
exit "$RC"
