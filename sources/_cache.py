# -*- coding: utf-8 -*-
"""Shared persistence + caching primitives for the sources/ overlay framework.

Every fetcher in sources/ is an INFORMATIONAL OVERLAY producer (never a scorer).
These helpers give them a uniform, graceful-skip way to:
  * persist small JSON state across cron runs (load_state / save_state)
  * archive a daily snapshot and merge history (archive_snapshot / load_archive)
  * TTL-cache an expensive/network fetch with last-good fallback (cached_fetch)

Idioms copied from chip_state.py: load → return a default on any failure;
save → makedirs(dirname) first. GOTCHA carried over: os.path.dirname() of a
bare filename ("x.json") is "" and os.makedirs("") raises FileNotFoundError —
so we ALWAYS abspath() before taking dirname (see save_state).
"""
import json
import os


def load_state(path, default=None):
    """JSON-load `path`; on missing/corrupt/any error return `default`.

    default of None is normalised to {} (matches the contract: dict default).
    """
    if default is None:
        default = {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_state(path, data):
    """JSON-dump `data` to `path`, creating parent dirs first.

    Wraps in os.path.abspath so a bare filename ("x.json") still yields a real
    parent dir for makedirs instead of "" (which would raise).
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def archive_snapshot(archive_dir, date_key, rows):
    """Write `rows` as <archive_dir>/<date_key>.json (makedirs first). Returns path."""
    os.makedirs(os.path.abspath(archive_dir), exist_ok=True)
    out = os.path.join(archive_dir, "%s.json" % date_key)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return out


def load_archive(archive_dir):
    """Merge every <archive_dir>/*.json into {date_key: rows}. {} if dir missing.

    date_key = filename without the .json suffix. Corrupt / unreadable files are
    skipped (graceful) so one bad snapshot never breaks the merge.
    """
    result = {}
    if not os.path.isdir(archive_dir):
        return result
    for name in os.listdir(archive_dir):
        if not name.endswith(".json"):
            continue
        date_key = name[: -len(".json")]
        try:
            with open(os.path.join(archive_dir, name), encoding="utf-8") as f:
                result[date_key] = json.load(f)
        except Exception:
            continue
    return result


def cached_fetch(cache_path, key, ttl_sec, now_ts, fetch_fn):
    """TTL cache over `fetch_fn` keyed by `key` inside the JSON at `cache_path`.

    * load_state(cache_path); if key present and (now_ts - entry['ts'] < ttl_sec)
      → return the cached entry['val'] (cache HIT, no fetch).
    * else try fetch_fn(); on success store {ts: now_ts, val: result} & save,
      return result.
    * on Exception during fetch → return the last cached val if any, else None
      (graceful-skip: a dead source never crashes the pipeline).
    """
    state = load_state(cache_path, {})
    entry = state.get(key)
    if isinstance(entry, dict) and "ts" in entry and (now_ts - entry["ts"] < ttl_sec):
        return entry.get("val")
    try:
        result = fetch_fn()
    except Exception:
        # fall back to the last cached value if we have one, else None
        if isinstance(entry, dict) and "val" in entry:
            return entry["val"]
        return None
    state[key] = {"ts": now_ts, "val": result}
    save_state(cache_path, state)
    return result
