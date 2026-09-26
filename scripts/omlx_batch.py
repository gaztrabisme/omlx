#!/usr/bin/env python3
"""Schema-enforced batch extraction against oMLX, with an audit trail.

The thing between "here are the principles" and "here is a transport". Every project that
uses the local model for classification / extraction / scoring rewrites this same 100 lines;
this is that file, with the skill's gates already wired in:

  * schema enforcement is mandatory and PROVEN per item (meta["enforcement"] is recorded,
    not assumed — a parseable reply is not evidence a grammar ran)
  * an abstention branch is REQUIRED in the schema, because a forced shape manufactures
    answers for inputs that don't have one
  * worker count defaults to the server's scheduler capacity, and --workers 1 is the
    documented setting for anything you will compare across runs
  * every record — including abstentions, degradations and failures — lands in a JSONL
    audit trail, which is the cheapest eval set you will ever build

Usage:
    omlx_batch.py --model <id> --schema schema.json --prompt prompt.txt \\
                  --input items.txt --out results.jsonl [--workers 8] [--abstain-key found]

    --input     one item per line (or a .json/.jsonl array of strings / objects)
    --prompt    a template file; "{item}" for scalar items, or named fields ({query},
                {passage}, ...) when items are objects
    --schema    JSON Schema; MUST contain a boolean abstention key (see --abstain-key)
    --out       JSONL, appended and flushed per item so a Ctrl-C keeps what finished.
                Records are therefore in COMPLETION order, not input order — each carries
                `idx` (0-based input position); sort by it if you need to re-pair.

Exit codes: 0 all items produced enforced output · 1 any item failed · 2 bad arguments.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import omlx_client as oc  # noqa: E402


def load_items(path: Path) -> list:
    if path.suffix == ".jsonl":
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise ValueError(f"{path} must contain a JSON array")
        return data
    return [l for l in path.read_text().splitlines() if l.strip()]


def check_abstention(schema: dict, key: str) -> None:
    """Refuse to run a schema that cannot say 'I don't know'.

    This is the skill's hardest-won lesson, enforced rather than documented: a forced shape
    with no abstention branch invents an answer for every input, and the invented ones are
    indistinguishable from the real ones downstream.
    """
    props = schema.get("properties") or {}
    if key not in props:
        raise ValueError(
            f"schema has no abstention key {key!r}. Add a boolean {key!r} (plus a free-text "
            "'reason') and make it the ONLY required field, so the model can decline. "
            "Override the key name with --abstain-key, but do not remove the branch.\n"
            "NOTE: the abstention key must mean 'could I assess this at all?', NOT your "
            "verdict. If you name your verdict field here, a substantive negative "
            "('no, this does not match') gets logged as an abstention and your counts are "
            "wrong. Use a separate key (e.g. 'assessable') and leave the verdict optional.")
    if (props[key] or {}).get("type") != "boolean":
        raise ValueError(
            f"abstention key {key!r} must be type boolean (got "
            f"{(props[key] or {}).get('type')!r}). A non-boolean silently coerces — a string "
            "enum would make every record read as 'did not abstain', forever.")
    required = schema.get("required") or []
    if required and key not in required:
        raise ValueError(f"{key!r} must be in 'required' — otherwise the model can omit it.")
    extra_required = [k for k in required if k != key]
    if extra_required:
        raise ValueError(
            f"'required' also lists {extra_required}, which forces the model to produce them "
            f"even when {key} is false. Require only {key!r}; leave the payload fields "
            "optional so abstention is actually reachable.")


_STATUS_CACHE: dict = {"t": 0.0, "v": None}
_STATUS_LOCK = threading.Lock()


def server_sample(max_age=15.0) -> dict | None:
    """Cheap, time-cached `/api/status` sample stamped onto each record.

    Without this the audit trail records `seconds` per item and nothing about *why* an item
    was slow — so a foreign tenant taking the scheduler mid-run is invisible after the fact.
    """
    now = time.time()
    with _STATUS_LOCK:
        if _STATUS_CACHE["v"] is not None and now - _STATUS_CACHE["t"] < max_age:
            return _STATUS_CACHE["v"]
    try:
        root = oc.endpoint().rsplit("/v1", 1)[0]
        req = urllib.request.Request(root + "/api/status",
                                     headers={"Authorization": f"Bearer {oc.api_key()}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            s = json.load(r)
        active = s.get("active_requests")
        cap = s.get("max_concurrent_requests") or 8
        v = {"active": active, "waiting": s.get("waiting_requests"),
             "loaded": s.get("loaded_models"),
             "saturated": bool(active is not None and active >= cap)}
    except Exception as e:
        v = {"error": f"{type(e).__name__}"}
    with _STATUS_LOCK:
        _STATUS_CACHE.update(t=now, v=v)
    return v


def render(template: str, item) -> str:
    """`{item}` for scalars; named placeholders for dict items ({query}, {passage}, ...).

    A dict item still fills `{item}` with its JSON, so single-placeholder templates keep
    working — but multi-field tasks don't have to smuggle everything through one blob.
    """
    if isinstance(item, dict):
        fields = {k: ("" if v is None else str(v)) for k, v in item.items()}
        fields.setdefault("item", json.dumps(item, ensure_ascii=False))
        try:
            return template.format_map(fields)
        except KeyError as e:
            raise KeyError(
                f"template references {e} which is not a key of the item "
                f"(available: {sorted(fields)})") from None
    return template.replace("{item}", item if isinstance(item, str) else json.dumps(item))


def run_one(item, *, model, template, schema, name, abstain_key, max_tokens, temperature,
            timeout, stamp=None, idx=None) -> dict:
    rec = {"idx": idx, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "item": item, "ok": False,
           "abstained": None, "result": None, "enforcement": None, "warnings": None,
           "error": None, "seconds": None, "server": stamp}
    t0 = time.time()
    try:
        out, meta = oc.chat_meta([{"role": "user", "content": render(template, item)}], model,
                                 schema=schema, name=name, temperature=temperature,
                                 max_tokens=max_tokens, timeout=timeout)
        # enforcement + warnings ARE the Output Contract's conditions 1 and 2. Recording them
        # is what makes this file auditable rather than merely reassuring.
        rec["enforcement"], rec["warnings"] = meta["enforcement"], meta["warnings"]
        obj = json.loads(out)
        if abstain_key not in obj:
            raise ValueError(f"missing abstention key {abstain_key!r}: {obj}")
        rec.update(ok=True, result=obj, abstained=not bool(obj[abstain_key]))
    except oc.EnforcementDegraded as e:
        rec["error"] = f"EnforcementDegraded: {e}"
    except oc.SchemaRejected as e:
        rec["error"] = f"SchemaRejected: {e}"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "omlx batch").split("\n")[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--schema", type=Path, required=True)
    ap.add_argument("--prompt", type=Path, required=True,
                    help="template file containing {item}")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", default="record")
    ap.add_argument("--abstain-key", default="found")
    ap.add_argument("--workers", type=int, default=8,
                    help="match scheduler.max_concurrent_requests; use 1 for anything you "
                         "will compare across runs (batching breaks greedy reproducibility)")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="override the server's max_tokens (default: send nothing)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="override the model's temperature (default: send nothing)")
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args(argv)

    schema = json.loads(a.schema.read_text())
    try:
        check_abstention(schema, a.abstain_key)
    except ValueError as e:
        print(f"XX {e}", file=sys.stderr)
        return 2
    template = a.prompt.read_text()
    if "{" not in template:
        print("XX prompt template must contain {item} (or named fields for dict items)",
              file=sys.stderr)
        return 2
    items = load_items(a.input)
    if not items:
        print("XX no items", file=sys.stderr)
        return 2

    print(f"-- {len(items)} items · model {a.model} · workers {a.workers} · temp {a.temperature if a.temperature is not None else 'server'}")
    if a.workers > 1:
        print("   note: concurrent runs are NOT byte-reproducible; use --workers 1 to compare.")

    t0 = time.time()
    lock = threading.Lock()
    out_f = a.out.open("w")

    def work(pair):
        # A foreign tenant arriving mid-run is the top confounder and leaves no trace
        # otherwise, so every record carries a server sample: after the fact you can tell a
        # slow item from a stolen scheduler slot, and which half of the run to discard.
        i, it = pair
        rec = run_one(it, model=a.model, template=template, schema=schema, name=a.name,
                      abstain_key=a.abstain_key, max_tokens=a.max_tokens,
                      temperature=a.temperature, timeout=a.timeout, stamp=server_sample(),
                      idx=i)
        with lock:                       # append as we go — a Ctrl-C at item 199/200 must
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")   # not lose the trail
            out_f.flush()
        return rec

    try:
        records = oc.fanout(list(enumerate(items)), work, workers=a.workers)
    finally:
        out_f.close()
    wall = time.time() - t0

    saturated = [r for r in records if (r.get("server") or {}).get("saturated")]
    if saturated:
        print(f"WARN {len(saturated)}/{len(records)} items ran while the scheduler was "
              "saturated by other traffic — their timings are confounded (see `server` in "
              "the audit trail).")

    ok = [r for r in records if r["ok"]]
    abstained = [r for r in ok if r["abstained"]]
    unenforced = [r for r in ok if r["enforcement"] != "json_schema"]
    failed = [r for r in records if not r["ok"]]

    print(f"-- {len(ok)}/{len(records)} ok · {len(abstained)} abstained · "
          f"{len(failed)} failed · {wall:.1f}s wall "
          f"({wall / max(1, len(items)):.1f}s/item effective)")
    print(f"   audit trail: {a.out}")
    if unenforced:
        print(f"XX {len(unenforced)} items were NOT schema-enforced — the output is coaxed, "
              "not constrained. Treat it as untrusted.")
    for r in failed[:5]:
        print(f"   FAIL {str(r['item'])[:60]!r} -> {r['error'][:120]}")
    if len(failed) > 5:
        print(f"   ... and {len(failed) - 5} more (see {a.out})")
    return 1 if (failed or unenforced) else 0


if __name__ == "__main__":
    sys.exit(main())
