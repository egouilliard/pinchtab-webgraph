#!/usr/bin/env python3
"""
The ARTIFACT STORE — where a flow's downloads land, and how it knows what it already has.

Content-addressed, because the polling case demands it. "Download the report every 10
seconds" is never really that: it is *"tell me when a NEW report appears."* A flow that
re-downloads the same PDF 8,640 times a day and calls each one a result is useless. So every
saved file is hashed, and a hash the store has seen before is reported as a `dupe` rather
than saved again — which turns a dumb poller into a change detector for free.

The dedupe ledger is per-scope and PERSISTS ACROSS RUNS (that is the whole point — run N
must know what run N-1 already fetched). Scope defaults to the flow name, so two flows don't
poison each other's ledger.

Layout (under $PINCHTAB_WEBGRAPH_HOME, default ~/.pinchtab-webgraph):

    artifacts/
      <scope>/
        ledger.jsonl          append-only: one line per accepted artifact
        files/<sha256>.<ext>  the bytes, content-addressed (never overwritten)

Storing by hash rather than by filename also fixes the silent-corruption case where a site
serves ten different files all called `export.pdf`.
"""
import hashlib
import json
import os
import re
import time

_SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CHUNK = 1 << 20


def home_dir():
    # EXACTLY cache_store.home_dir()'s expression: an env-var path must be expanduser'd too
    # (`PINCHTAB_WEBGRAPH_HOME=~/somewhere` is a directory literally named `~` otherwise).
    return os.path.expanduser(os.environ.get("PINCHTAB_WEBGRAPH_HOME", "~/.pinchtab-webgraph"))


def artifacts_dir():
    return os.path.join(home_dir(), "artifacts")


def validate_scope(scope):
    """Scope is used as a directory segment, so it gets the same treatment as a cache host:
    an allowlist, not an escape (see cache_store.validate_host — same class of bug)."""
    scope = (scope or "default").strip()
    if not _SCOPE_RE.match(scope) or scope.strip(".") == "":
        raise ValueError("invalid artifact scope %r" % scope)
    return scope


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class ArtifactStore:
    """Content-addressed store + a persistent dedupe ledger.

    `root` is injectable so tests (and, later, a per-tenant worker) get an isolated store
    without touching the user's real one."""

    def __init__(self, scope="default", root=None):
        self.scope = validate_scope(scope)
        self.root = root or os.path.join(artifacts_dir(), self.scope)
        self.files_dir = os.path.join(self.root, "files")
        self.ledger_path = os.path.join(self.root, "ledger.jsonl")
        os.makedirs(self.files_dir, exist_ok=True)
        self._seen = self._load_ledger()

    def _load_ledger(self):
        seen = {}
        if not os.path.exists(self.ledger_path):
            return seen
        with open(self.ledger_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # a torn last line from a killed run — skip, don't die
                if rec.get("sha256"):
                    seen[rec["sha256"]] = rec
        return seen

    def has(self, sha):
        return sha in self._seen

    def staging_path(self, name):
        """Where a download is written BEFORE it is hashed. Downloads must land somewhere
        first — we can't know the content hash until we have the bytes."""
        staging = os.path.join(self.root, "staging")
        os.makedirs(staging, exist_ok=True)
        return os.path.join(staging, os.path.basename(name) or "download")

    def accept(self, staged_path, *, name=None, source=None, dedupe=True):
        """Hash a staged file and either admit it or recognise it as a duplicate.

        Returns {status: "new"|"dupe", sha256, path, name, size, ...}. On `dupe` the staged
        copy is removed and `path` points at the ALREADY-STORED bytes, so a caller can still
        reference the file it (re-)found without a second copy on disk."""
        sha = hash_file(staged_path)
        size = os.path.getsize(staged_path)
        name = name or os.path.basename(staged_path)

        if dedupe and self.has(sha):
            prior = self._seen[sha]
            os.remove(staged_path)
            return {"status": "dupe", "sha256": sha, "name": name, "size": size,
                    "path": prior.get("path"), "first_seen": prior.get("seen_at")}

        ext = os.path.splitext(name)[1][:12]
        final = os.path.join(self.files_dir, sha + ext)
        if os.path.exists(final):
            os.remove(staged_path)        # same bytes already stored under another ledger
        else:
            os.replace(staged_path, final)

        rec = {"sha256": sha, "name": name, "path": final, "size": size,
               "source": source, "seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(self.ledger_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        self._seen[sha] = rec
        return dict(rec, status="new")

    def stats(self):
        return {"scope": self.scope, "root": self.root, "count": len(self._seen),
                "bytes": sum(r.get("size") or 0 for r in self._seen.values())}
