#!/usr/bin/env python3
"""Draft a pack manifest from a GitHub repository (stdlib only).

  add.py owner/repo [--asset SUBSTR] [--id ID] [--author NAME] [--kind KIND] [--tags a,b]
  add.py owner/repo --commit [SHA]     pack the repo itself (archive at a commit) when it has no release

Takes the newest stable release's archive asset (or the repo archive at a commit), downloads and
surveys it, and writes packs/<id>.json, or prepends a version to an existing manifest. Needs a
GitHub token from GITHUB_TOKEN or a logged-in `gh`. Read the result and edit the description,
author, kind and tags before opening a PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as reg  # noqa: E402

API = "https://api.github.com"
SPDX = {"CC0-1.0", "CC-BY-3.0", "CC-BY-4.0", "CC-BY-SA-3.0", "CC-BY-SA-4.0", "Unlicense", "MIT"}


def token() -> str | None:
    if t := os.environ.get("GITHUB_TOKEN"):
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def gh(path: str):
    t = token()
    req = urllib.request.Request(API + path, headers={"User-Agent": reg.UA, "Accept": "application/vnd.github+json",
                                                     **({"Authorization": f"Bearer {t}"} if t else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pick_release(repo: str, substr: str | None) -> tuple[str, dict]:
    for rel in gh(f"/repos/{repo}/releases?per_page=20"):
        if rel["prerelease"] or rel["draft"]:
            continue
        assets = [a for a in rel["assets"] if reg.archive_kind_of(a["name"])
                  and (substr is None or substr.lower() in a["name"].lower())]
        if assets:
            return rel["tag_name"], max(assets, key=lambda a: a["size"])
    raise SystemExit(f"{repo}: no stable release with a .zip/.tar archive" + (f" matching '{substr}'" if substr else ""))


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:50].strip("-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("--asset", help="pick the release asset whose name contains this")
    ap.add_argument("--commit", nargs="?", const="HEAD", help="use the repo archive at this commit (default: latest)")
    ap.add_argument("--root", help="folder inside the archive to install (default: its single wrapper folder)")
    ap.add_argument("--id")
    ap.add_argument("--name")
    ap.add_argument("--author")
    ap.add_argument("--kind", choices=sorted(reg.KINDS), default="multisample")
    ap.add_argument("--tags", default="")
    ap.add_argument("--license", help="SPDX id, when GitHub can't detect it")
    args = ap.parse_args()

    info = gh(f"/repos/{args.repo}")
    lic = args.license or (info.get("license") or {}).get("spdx_id")
    if lic not in SPDX:
        raise SystemExit(f"{args.repo}: license '{lic}' isn't allowed (pass --license if GitHub got it wrong)")

    if args.commit:
        sha = gh(f"/repos/{args.repo}/commits/{args.commit if args.commit != 'HEAD' else info['default_branch']}")["sha"]
        url = f"https://github.com/{args.repo}/archive/{sha}.zip"
        version = sha[:7]
    else:
        tag, asset = pick_release(args.repo, args.asset)
        url = asset["browser_download_url"]
        version = tag.lstrip("vV")

    path = reg.download(url)
    kind = reg.archive_kind_of(url)
    with tempfile.TemporaryDirectory(prefix="nota-add-") as tmp:
        top = Path(tmp) / "x"
        reg.unpack(path, kind, top)
        root = args.root if args.root is not None else reg.suggest_root(top)
        s = reg.survey(top / root if root else top)
    if not s["files"]:
        raise SystemExit(f"{args.repo}: no audio files in {url}")
    asset_block = {"url": url, "sha256": reg.sha256(path), "size": path.stat().st_size}
    if root:
        asset_block["root"] = root
    asset_block.update(files=s["files"], unpackedSize=s["unpackedSize"], formats=s["formats"])
    entry = {"version": version, "asset": asset_block}

    pid = args.id or slug(info["name"].split(".", 1)[-1] if "." in info["name"] else info["name"])
    out = reg.PACKS / f"{pid}.json"
    if out.exists():
        m = json.loads(out.read_text(encoding="utf-8"))
        if any(v["version"] == version for v in m["versions"]):
            print(f"{pid}: {version} already listed")
            return 0
        m["versions"].insert(0, entry)
    else:
        m = {
            "id": pid,
            "name": args.name or info["name"],
            "author": args.author or info["owner"]["login"],
            "description": (info.get("description") or "")[:200],
            **({"homepage": info["homepage"]} if (info.get("homepage") or "").startswith("https://") else {}),
            "source": info["html_url"],
            "license": lic,
            "kind": args.kind,
            "tags": [t for t in args.tags.split(",") if t],
            "versions": [entry],
        }
    reg.PACKS.mkdir(exist_ok=True)
    out.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(reg.ROOT)}: {version}, {s['files']} samples, {reg._mb(s['unpackedSize'])} unpacked"
          + (f", {len(s['skipped'])} file(s) skipped" if s["skipped"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
