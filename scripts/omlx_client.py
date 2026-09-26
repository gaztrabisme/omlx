#!/usr/bin/env python3
"""Minimal oMLX client — the request shape, auth, structured output, thinking control,
and transient retry in one place. Stdlib only; no `openai` dependency.

Verified against oMLX 0.5.3 on 2026-07-29. Re-run `omlx_probe.sh --deep` after an upgrade.

Two things this does that a hand-rolled POST usually doesn't, and that cost other projects
real debugging time:

  1. Sends the FULL `response_format` wrapper. `type` defaults to "text" server-side, so a
     request missing it is silently unenforced — the model just talks.
  2. Surfaces the HTTP `Warning` response header. When grammar compilation fails, oMLX does
     not error; it degrades to prompt-based JSON coaxing and sets that header. Ignoring it
     means you cannot tell enforced output from coaxed output.
  3. Reports what it actually SENT (`meta["enforcement"]`), because a well-formed JSON reply
     with the right keys is not evidence that a grammar ran — an unconstrained model will
     happily produce one.
  4. Refuses the near-misses: `guided_json` (accepted and ignored by oMLX) raises rather than
     silently returning prose, and a dead connection from an uncompilable schema raises
     SchemaRejected instead of a bare IncompleteRead.

Config (no secrets in this file):
    OMLX_ENDPOINT   default http://127.0.0.1:8000/v1
    OMLX_API_KEY    falls back to `auth.api_key` in $OMLX_BASE_PATH (default ~/.omlx)/settings.json
    OMLX_MODEL      optional default model

CLI smoke test:
    python3 omlx_client.py --model <id> --prompt "hi"
    python3 omlx_client.py --model <id> --prompt "..." --schema schema.json
    python3 omlx_client.py --list
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1"
BASE_PATH = Path(os.environ.get("OMLX_BASE_PATH", "~/.omlx")).expanduser()

# Transport-level failures worth retrying. NOTE HTTPError is a subclass of URLError and is
# handled separately BEFORE this tuple — most HTTP errors are refusals that retrying cannot
# fix (a 507 memory refusal retried just re-pressures a box that is already out of memory).
_TRANSIENT = (urllib.error.URLError, TimeoutError, OSError)

# The only statuses where a retry is meaningful.
_RETRY_STATUS = {408, 429, 502, 503, 504}


class EnforcementDegraded(RuntimeError):
    """oMLX fell back from grammar-constrained decoding to prompt coaxing (Warning header)."""


class SchemaRejected(RuntimeError):
    """The constraint engine (xgrammar) could not compile the schema.

    Symptom is usually a dead connection (IncompleteRead) with no status, no body and no
    Warning header — the server logs the reason and the client sees nothing. Retrying does
    not help; the schema itself is the problem.
    """


class ServerRefused(RuntimeError):
    """The server returned an HTTP error and told you why. Carries `.status` and `.detail`.

    The message oMLX puts in the body is usually the actionable part — e.g. a 507 says
    "projected memory NN GB would exceed the memory ceiling NN GB … Free system memory or
    lower memory_guard_tier". A bare `HTTPError: 507 Insufficient Storage` throws that away.
    """

    def __init__(self, status, detail, url):
        self.status, self.detail = status, detail
        hint = ""
        if status == 507:
            hint = ("  [memory-guard admission refusal: the model cannot be loaded alongside "
                    "what is already resident. Unload another model, or lower "
                    "memory_guard_tier. Retrying will not help.]")
        super().__init__(f"HTTP {status} from {url}: {detail}{hint}")


# Fields that LOOK like they constrain decoding and do not. Passing one is silently
# unenforced output, which is the failure this client exists to prevent.
_IGNORED_CONSTRAINT_FIELDS = {"guided_json"}


def endpoint() -> str:
    return os.environ.get("OMLX_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def api_key() -> str:
    """Env first, then the server's own settings file. Never a literal in this repo."""
    key = os.environ.get("OMLX_API_KEY")
    if key:
        return key
    settings = BASE_PATH / "settings.json"
    try:
        return json.loads(settings.read_text())["auth"]["api_key"]
    except Exception as e:
        raise RuntimeError(
            f"No API key: set OMLX_API_KEY, or make {settings} readable with auth.api_key"
        ) from e


def _post(path: str, payload: dict, timeout: int, retries: int) -> tuple[dict, list[str]]:
    """POST JSON, return (body, warning_headers). Retries transport errors only."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"}
    url = f"{endpoint()}{path}"
    last = None
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), r.headers.get_all("Warning") or []
        except http.client.IncompleteRead as e:
            # Not transient: a schema the constraint engine can't compile kills the stream
            # with no status, no body and no Warning header.
            raise SchemaRejected(
                "connection died mid-response with no status or Warning header. The usual "
                "cause is a schema the constraint engine rejected (bad `type`, dangling "
                "$ref, regex lookahead). Check the server log for "
                "'grammar-constrained decoding is unavailable'."
            ) from e
        except urllib.error.HTTPError as e:
            # Read the body BEFORE deciding — it holds the only actionable text.
            try:
                raw = e.read().decode("utf-8", "replace")
                detail = json.loads(raw).get("error", {}).get("message") or raw[:400]
            except Exception:
                detail = getattr(e, "reason", "") or "no body"
            if e.code in _RETRY_STATUS:
                last = ServerRefused(e.code, detail, url)
                continue
            raise ServerRefused(e.code, detail, url) from e
        except _TRANSIENT as e:
            last = e
    raise last or RuntimeError(f"omlx: no attempts made for {path}")


def chat_meta(messages, model=None, *, schema=None, name="out", temperature=None,
              max_tokens=None, thinking=None, thinking_budget=None, timeout=180, retries=2,
              strict_enforcement=True, extra=None) -> tuple[str, dict]:
    """One chat completion → (assistant text, meta).

    meta = {"enforcement", "warnings", "tool_calls", "finish_reason", "usage"}.
    `enforcement` reports what was actually SENT ("json_schema" | "structured_outputs" |
    "grammar" | "none") — the half of the proof a response alone cannot give you.
    Well-formed JSON with the right keys is not evidence that a grammar ran.

    schema:   JSON Schema dict → sends the full json_schema wrapper. `type` is what enforces;
              omit it and the server silently returns unconstrained prose. `name` is required
              by the spec — always send it — but on 0.5.3 omitting it still enforced.
    temperature, max_tokens, thinking, thinking_budget: None (default) sends nothing, so
              the server's per-model settings (~/.omlx/model_settings.json) and global
              `sampling` block apply. Standing rule: leave them None and change the server
              settings instead (Gary, 2026-09-26). They remain for a caller who must
              override, and a non-None value is sent exactly as given.
    strict_enforcement: with a schema, raise EnforcementDegraded if the server signalled it
              fell back to prompt coaxing. Set False to accept degraded output knowingly.
    """
    payload = {"model": model or os.environ.get("OMLX_MODEL"), "messages": messages}
    if not payload["model"]:
        raise ValueError("no model: pass model= or set OMLX_MODEL")
    # Generation parameters are sent only when a caller explicitly overrides them.
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if thinking_budget is not None:
        payload["thinking_budget"] = thinking_budget
    if thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    if schema is not None:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": name, "schema": schema}}
    if extra:
        bogus = _IGNORED_CONSTRAINT_FIELDS & set(extra)
        if bogus:
            raise ValueError(
                f"{sorted(bogus)} is accepted by oMLX and silently ignored (it is vLLM "
                "syntax) — output would be unenforced with no error and no warning. Pass "
                "schema= instead.")
        payload.update(extra)

    # What did we actually ask for? Half the enforcement proof lives here, not in the response.
    rf = payload.get("response_format") or {}
    if rf.get("type") == "json_schema" and (rf.get("json_schema") or {}).get("schema"):
        enforcement = "json_schema"
    elif (payload.get("structured_outputs") or {}).get("json"):
        enforcement = "structured_outputs"      # also enforces on 0.5.3, verified
    elif payload.get("structured_outputs") or payload.get("guided_grammar"):
        enforcement = "grammar"
    else:
        enforcement = "none"

    data, warnings = _post("/chat/completions", payload, timeout, retries)
    if schema is not None and warnings and strict_enforcement:
        raise EnforcementDegraded(
            "oMLX degraded to prompt-based JSON coaxing — output is NOT schema-enforced: "
            + "; ".join(warnings))

    choice = data["choices"][0]
    msg = choice["message"]
    text = msg.get("content") or ""       # absent entirely on a tool-call response
    return text, {"enforcement": enforcement, "warnings": warnings,
                  "tool_calls": msg.get("tool_calls") or [],
                  "finish_reason": choice.get("finish_reason"),
                  "usage": data.get("usage")}


def _flag(value):
    return None if value is None else value == "on"


def chat(messages, model=None, **kw) -> str:
    """chat_meta() → just the assistant text. Use chat_meta() when you need the proof."""
    return chat_meta(messages, model, **kw)[0]


def chat_json(messages, model=None, *, require=(), with_meta=False, **kw):
    """chat() + parse + key validation.

    Keep this validation even under schema enforcement: enforcement can silently degrade,
    and 'skip validation because structured mode is on' is a named anti-pattern. The
    brace-slice is a belt against a stray prefix/suffix, not the primary mechanism.

    Refuses to run unconstrained — and refuses BEFORE spending the inference, not after.
    Parsing JSON out of a free-form reply and calling it "structured output" is exactly the
    proxy the Output Contract forbids. Pass a schema, or call chat() and own the parsing.

    with_meta=True returns (obj, meta) so you can record `meta["enforcement"]` and
    `meta["warnings"]` — conditions 1 and 2 of the Output Contract. Without it the proof is
    unrecordable, so prefer it for anything you will have to evidence later.
    """
    if kw.get("schema") is None and not (kw.get("extra") or {}).get("structured_outputs"):
        raise ValueError(
            "chat_json called with no decoding constraint — pass schema=<JSON Schema>. "
            "A parseable reply is not evidence that a grammar ran.")
    text, meta = chat_meta(messages, model, **kw)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        try:
            obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(f"no JSON object in response: {text[:200]!r}") from e
    missing = [k for k in require if k not in obj]
    if missing:
        raise ValueError(f"response missing required keys {missing}: {obj}")
    return (obj, meta) if with_meta else obj


def embed(texts, model=None, *, batch=32, timeout=180, retries=2, dimensions=None,
          max_length=None, truncation=True, encoding_format=None) -> list[list[float]]:
    """Embed a list of strings. Batched client-side; the server also batches internally.

    `truncation` defaults to TRUE server-side: an input longer than the embedding model's
    context is **silently cut**, with no error and no flag on the response. That is the same
    coverage-loss class as sparse prefill, and there is no signal for it — so if your chunks
    may be long, either pass truncation=False (to get an error instead of a quiet cut) or
    bound them yourself before calling.

    Note also that retrieval checkpoints are often trained with asymmetric query/passage
    encoding. If yours is, this plain call encodes both the same way and you will leave
    recall on the table — check the model card.
    """
    model = model or os.environ.get("OMLX_EMBED_MODEL")
    if not model:
        raise ValueError("no embedding model: pass model= or set OMLX_EMBED_MODEL")
    opts = {k: v for k, v in (("dimensions", dimensions), ("max_length", max_length),
                              ("truncation", truncation),
                              ("encoding_format", encoding_format)) if v is not None}
    out = []
    for i in range(0, len(texts), batch):
        data, _ = _post("/embeddings", {"model": model, "input": texts[i:i + batch], **opts},
                        timeout, retries)
        out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
    return out


def models() -> list[dict]:
    req = urllib.request.Request(f"{endpoint()}/models",
                                 headers={"Authorization": f"Bearer {api_key()}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["data"]


def health(timeout: int = 5) -> bool:
    """Unauthenticated liveness check."""
    root = endpoint().rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(f"{root}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def fanout(items, fn, workers=8):
    """Order-preserving concurrent map. Match `workers` to the server's
    scheduler.max_concurrent_requests (default 8) — more threads only queue.

    Expect 1.4x on long prefill-bound prompts, ~2.6-2.9x on short-prompt structured work.
    Never 8x. Use workers=1 for anything you will compare across runs: continuous batching
    breaks greedy reproducibility (measured 10-11/12 records identical at 8 workers vs
    12/12 sequentially).
    """
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def _main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="oMLX smoke test")
    ap.add_argument("--model", default=os.environ.get("OMLX_MODEL"))
    ap.add_argument("--prompt")
    ap.add_argument("--schema", type=Path, help="path to a JSON Schema file")
    ap.add_argument("--name", default="out")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="override the server's max_tokens (default: send nothing)")
    ap.add_argument("--thinking", choices=("on", "off"), default=None,
                    help="override the model's thinking setting (default: send nothing)")
    ap.add_argument("--list", action="store_true", help="list served models and exit")
    a = ap.parse_args(argv)

    if a.list:
        for m in models():
            print(f"{m['id']}\tctx={m.get('max_model_len')}")
        return 0
    if not a.prompt or not a.model:
        ap.error("--prompt and --model are required (or use --list)")

    msgs = [{"role": "user", "content": a.prompt}]
    if a.schema:
        schema = json.loads(a.schema.read_text())
        obj = chat_json(msgs, a.model, schema=schema, name=a.name,
                        max_tokens=a.max_tokens, thinking=_flag(a.thinking),
                        require=tuple(schema.get("required", ())))
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(chat(msgs, a.model, max_tokens=a.max_tokens, thinking=_flag(a.thinking)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except EnforcementDegraded as e:
        print(f"XX {e}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"XX {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
