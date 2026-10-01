#!/usr/bin/env python3
"""Compat runner for zens_ink_pro.full_audit.

Pro v2.6.0 bundles its own vendored zens_ink whose behavior differs from the
standalone free repo clone in two seams that break the 12-step pipeline:

  1. pro passes plain strings to site_audit.run(); free's run() is Path-typed
  2. free's keyword_volume.get_stats() sys.exit(1)s on a missing BING key,
     killing the whole subprocess; pro's vendored copy degrades to zeros
     (pro even prints "WARNING: scores will lack volume data" for this case)

This wrapper shims both, across every loaded zens_ink* namespace (bindings
are from-imported into several modules), then runs the stock pro CLI main().
"""
import json
import sys
from pathlib import Path

import zens_ink_pro.full_audit as FA

# ── seam 1: str → Path + json-str → dict ────────────────────────────────────
_orig_run = getattr(FA, "site_audit_run", None)
if _orig_run is not None:
    def _shim(dist_dir, sitemap_path=None, base_path="", output_format="json", **kw):
        out = _orig_run(
            Path(dist_dir),
            Path(sitemap_path) if sitemap_path else Path("__no_sitemap__"),
            base_path=base_path, output_format=output_format, **kw,
        )
        if output_format == "json" and isinstance(out, str):
            try:
                return json.loads(out)
            except ValueError:
                return out
        return out
    FA.site_audit_run = _shim

# ── seam 2: sys.exit on missing BING key → graceful zeros ───────────────────
_EMPTY_STATS = {"avg_weekly": 0, "latest_weekly": 0, "trend": "no_data",
                "quarterly": 0, "weeks": 0}

def _patch_noexit():
    for name, mod in list(sys.modules.items()):
        if not name.startswith("zens_ink") or mod is None:
            continue
        for attr in ("get_stats", "get_volume"):
            fn = getattr(mod, attr, None)
            if callable(fn) and not getattr(fn, "_noexit", False):
                def wrapped(*a, __fn=fn, **k):
                    try:
                        return __fn(*a, **k)
                    except SystemExit:
                        out = dict(_EMPTY_STATS)
                        out["keyword"] = a[0] if a else ""
                        return out
                wrapped._noexit = True
                setattr(mod, attr, wrapped)

_patch_noexit()

if __name__ == "__main__":
    FA.main()
