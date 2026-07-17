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
import gzip
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
    """Write `rows` as gzip-compressed <archive_dir>/<date_key>.json.gz (makedirs first).

    Gzip'd (2026-07-18) because the raw daily/weekly snapshots dominate the archive's
    year-on-year growth (~9× shrink on the JSON). Returns the written path. A same-key
    re-write overwrites (the tdcc same-week-repull contract is preserved).
    """
    os.makedirs(os.path.abspath(archive_dir), exist_ok=True)
    out = os.path.join(archive_dir, "%s.json.gz" % date_key)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return out


def load_archive(archive_dir):
    """Merge every <archive_dir>/*.json[.gz] into {date_key: rows}. {} if dir missing.

    Accepts BOTH the gzip-compressed .json.gz form (current) and the legacy plain
    .json form (pre-2026-07-18 / test fixtures). date_key = filename minus the
    .json.gz or .json suffix. When both forms exist for one date_key the .gz wins
    (a converted snapshot supersedes any leftover plain original). Corrupt /
    unreadable files are skipped (graceful) so one bad snapshot never breaks the merge.
    """
    result = {}
    if not os.path.isdir(archive_dir):
        return result
    names = os.listdir(archive_dir)
    gz_keys = {n[: -len(".json.gz")] for n in names if n.endswith(".json.gz")}
    for name in names:
        if name.endswith(".json.gz"):
            date_key = name[: -len(".json.gz")]
        elif name.endswith(".json"):
            date_key = name[: -len(".json")]
            if date_key in gz_keys:
                continue                       # .gz form supersedes the plain original
        else:
            continue
        path = os.path.join(archive_dir, name)
        try:
            if name.endswith(".gz"):
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    result[date_key] = json.load(f)
            else:
                with open(path, encoding="utf-8") as f:
                    result[date_key] = json.load(f)
        except Exception:
            continue
    return result


def read_snapshot(archive_dir, date_key, default=None):
    """Load a SINGLE <archive_dir>/<date_key>.json[.gz] (gzip-aware). `default` (→ [])
    on absent/corrupt. Lets a caller read just one day/week without merging the whole
    archive (the O(codes×rows) hot path when only the latest prior snapshot is needed).
    """
    if default is None:
        default = []
    gz = os.path.join(archive_dir, "%s.json.gz" % date_key)
    plain = os.path.join(archive_dir, "%s.json" % date_key)
    try:
        if os.path.isfile(gz):
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                return json.load(f)
        if os.path.isfile(plain):
            with open(plain, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return default
    return default


def archive_keys(archive_dir):
    """Sorted date_keys present in `archive_dir` (accepts .json and .json.gz). [] if missing."""
    if not os.path.isdir(archive_dir):
        return []
    keys = set()
    for n in os.listdir(archive_dir):
        if n.endswith(".json.gz"):
            keys.add(n[: -len(".json.gz")])
        elif n.endswith(".json"):
            keys.add(n[: -len(".json")])
    return sorted(keys)


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
