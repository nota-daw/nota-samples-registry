#!/usr/bin/env python3
"""Nota sample registry tool (stdlib only).

  validate            static checks on every manifest in packs/
  build [--out DIR]   write DIR/index.json (what Nota downloads)
  verify [ID ...]     download each pack, check size + sha256, unpack, and check that its
                      contents match the manifest (file count, unpacked size, formats)
  inspect URL|FILE    download + hash + unpack one archive and print a draft "asset" block
                      (the quickest way to fill in a new manifest)
  outdated            compare packs hosted on GitHub releases with their latest release
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKS = ROOT / "packs"
CACHE = Path(os.environ.get("NOTA_REGISTRY_CACHE", ROOT / ".cache"))

SCHEMA_VERSION = 1
KINDS = {"one-shots", "loops", "multisample", "mixed"}
ARCHIVES = {"zip", "tar"}
# Audio Nota can load; these are counted as the pack's samples.
AUDIO = {"wav", "wave", "aif", "aiff", "flac", "mp3", "ogg"}
# What else Nota installs alongside the samples. Anything outside AUDIO + EXTRAS (synth presets,
# DAW projects, an .exe) is skipped when installing and doesn't count towards unpackedSize.
EXTRAS = {"txt", "md", "pdf", "rtf", "html", "htm", "sfz", "mid", "midi", "png", "jpg", "jpeg",
          "json", "xml", "csv", "nfo", ""}
# Archive cruft that Nota drops when installing and that doesn't count towards unpackedSize.
JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
# Licenses that let anyone use the samples in any music, commercial included. No NC/ND.
LICENSES = {"CC0-1.0", "CC-BY-3.0", "CC-BY-4.0", "CC-BY-SA-3.0", "CC-BY-SA-4.0", "Unlicense", "MIT"}
ATTRIBUTION_REQUIRED = {"CC-BY-3.0", "CC-BY-4.0", "CC-BY-SA-3.0", "CC-BY-SA-4.0", "MIT"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# Hosts whose files don't change under a fixed URL. GitHub repo archives are pinned by tag or commit.
GH_RELEASE_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/[^/]+/[^/]+$")
URL_RES = [
    GH_RELEASE_RE,
    re.compile(r"^https://github\.com/[^/]+/[^/]+/archive/(refs/tags/)?[^/]+\.(zip|tar\.gz)$"),
    re.compile(r"^https://archive\.org/download/[^/]+/.+$"),
]
PREVIEW_RES = URL_RES + [re.compile(r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/[0-9a-f]{40}/.+\.(ogg|mp3|wav|flac)$")]
UA = "nota-samples-registry"


# --- manifests ------------------------------------------------------------------

def load_manifests() -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(PACKS.glob("*.json")):
        with p.open(encoding="utf-8") as f:
            out.append((p, json.load(f)))
    return out


def archive_kind(asset: dict) -> str | None:
    if "archive" in asset:
        return asset["archive"]
    return archive_kind_of(asset.get("url", ""))


def archive_kind_of(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if re.search(r"\.(tar|tar\.gz|tgz|tar\.xz|txz|tar\.bz2)$", n):
        return "tar"
    return None


def validate_manifest(path: Path, m: dict) -> list[str]:
    errs: list[str] = []

    def err(msg: str) -> None:
        errs.append(f"{path.name}: {msg}")

    for key in ("id", "name", "author", "description", "source", "license", "kind", "versions"):
        if key not in m:
            err(f"missing '{key}'")
    if errs:
        return errs

    if not ID_RE.match(m["id"]):
        err(f"id '{m['id']}' must be lowercase letters, digits and dashes")
    if path.stem != m["id"]:
        err(f"file name must be '{m['id']}.json'")
    if not m["source"].startswith("https://"):
        err("source must be the https page where the author publishes the pack")
    if "homepage" in m and not m["homepage"].startswith("https://"):
        err("homepage must be an https URL")
    if m["license"] not in LICENSES:
        err(f"license '{m['license']}' is not in the allowed list {sorted(LICENSES)}")
    if m["license"] in ATTRIBUTION_REQUIRED and not m.get("attribution"):
        err(f"{m['license']} needs an 'attribution' line (credit as the author asks for it)")
    if len(m.get("attribution", "")) > 200:
        err("attribution must be at most 200 characters")
    if m["kind"] not in KINDS:
        err(f"kind must be one of {sorted(KINDS)}")
    if len(m["description"]) > 200:
        err("description must be at most 200 characters")
    if not isinstance(m.get("tags", []), list) or not all(isinstance(t, str) and t == t.lower() for t in m.get("tags", [])):
        err("tags must be a list of lowercase strings")
    if len(m.get("notes", "")) > 200:
        err("notes must be at most 200 characters")
    if "preview" in m and not any(r.match(m["preview"]) for r in PREVIEW_RES):
        err("preview must be an audio file on GitHub (release, or raw at a commit sha) or archive.org")
    if "bpm" in m and not (isinstance(m["bpm"], (int, float)) and 20 <= m["bpm"] <= 400):
        err("bpm must be a number between 20 and 400")
    unknown = set(m) - {"id", "name", "author", "description", "homepage", "source", "license", "attribution",
                        "kind", "tags", "notes", "preview", "bpm", "key", "versions"}
    if unknown:
        err(f"unknown field(s) {sorted(unknown)}")

    versions = m["versions"]
    if not isinstance(versions, list) or not versions:
        err("versions must be a non-empty list, newest first")
        return errs
    seen = set()
    for v in versions:
        ver = v.get("version")
        if not ver or ver in seen:
            err(f"version '{ver}' is missing or duplicated")
        seen.add(ver)
        a = v.get("asset")
        if not isinstance(a, dict):
            err(f"{ver}: missing 'asset'")
            continue
        where = f"{ver}"
        unknown = set(a) - {"url", "sha256", "size", "archive", "root", "files", "unpackedSize", "formats"}
        if unknown:
            err(f"{where}: unknown asset field(s) {sorted(unknown)}")
        if not any(r.match(a.get("url", "")) for r in URL_RES):
            err(f"{where}: url must be a GitHub release asset, a GitHub tag/commit archive or an archive.org download")
        if not SHA_RE.match(a.get("sha256", "")):
            err(f"{where}: sha256 must be 64 lowercase hex chars")
        for key in ("size", "files", "unpackedSize"):
            if not isinstance(a.get(key), int) or a[key] <= 0:
                err(f"{where}: {key} must be a positive integer")
        if archive_kind(a) not in ARCHIVES:
            err(f"{where}: can't tell the archive type; set 'archive' to one of {sorted(ARCHIVES)}")
        root = a.get("root")
        if root is not None and (not isinstance(root, str) or not root or _unsafe(root)):
            err(f"{where}: root must be a relative folder path inside the archive")
        formats = a.get("formats")
        if not isinstance(formats, list) or not formats or any(f not in AUDIO for f in formats) or formats != sorted(set(formats)):
            err(f"{where}: formats must be the sorted audio extensions in the pack, from {sorted(AUDIO)}")
    return errs


def _unsafe(rel: str) -> bool:
    return rel.startswith(("/", "\\")) or ".." in Path(rel).parts or ":" in rel


def cmd_validate(_args) -> int:
    manifests = load_manifests()
    errs: list[str] = []
    ids = set()
    for path, m in manifests:
        errs += validate_manifest(path, m)
        if m.get("id") in ids:
            errs.append(f"{path.name}: duplicate id")
        ids.add(m.get("id"))
    for e in errs:
        print("error:", e)
    print(f"{len(manifests)} manifest(s), {len(errs)} error(s)")
    return 1 if errs else 0


def cmd_build(args) -> int:
    if cmd_validate(args):
        return 1
    packs = [m for _, m in load_manifests()]
    packs.sort(key=lambda m: m["name"].lower())
    index = {
        "schema": SCHEMA_VERSION,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "packs": packs,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "index.html").write_text(_html(packs), encoding="utf-8")
    print(f"wrote {out / 'index.json'} ({len(packs)} packs)")
    return 0


def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1e7 else f"{n / 1e6:.1f} MB"


def _html(packs: list[dict]) -> str:
    from html import escape
    rows = "\n".join(
        f"<tr><td><a href='{escape(m['source'])}'>{escape(m['name'])}</a></td><td>{escape(m['author'])}</td>"
        f"<td>{escape(m['kind'])}</td><td>{m['versions'][0]['asset']['files']}</td>"
        f"<td>{_mb(m['versions'][0]['asset']['size'])}</td><td>{escape(m['license'])}</td></tr>"
        for m in packs)
    return ("<!doctype html><meta charset=utf-8><title>Nota sample registry</title>"
            "<style>body{font:14px system-ui;margin:2em}td,th{padding:4px 10px;text-align:left}</style>"
            "<h1>Nota sample registry</h1><p>Free sample packs installable from Nota. "
            "Machine-readable: <a href='index.json'>index.json</a>.</p>"
            f"<table><tr><th>Pack</th><th>Author</th><th>Kind</th><th>Samples</th><th>Download</th><th>License</th></tr>{rows}</table>\n")


# --- download + unpack ----------------------------------------------------------

def download(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / hashlib.sha1(url.encode()).hexdigest()[:12] / url.rsplit("/", 1)[-1]
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.rename(dest)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(archive: Path, kind: str, dest: Path) -> None:
    """Expands archive into dest (which must not exist yet). Rejects paths that escape it."""
    dest.mkdir(parents=True)
    if kind == "zip":
        # Some Windows-built zips store "a\\b\\c" paths; treat the backslash as a separator.
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                rel = info.filename.replace("\\", "/")
                if _unsafe(rel):
                    raise ValueError(f"unsafe path in archive: {info.filename}")
                target = dest / rel
                if rel.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
    elif kind == "tar":
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):  # Python 3.12+ (and security backports)
                t.extractall(dest, filter="data")
            else:
                for member in t.getmembers():
                    if _unsafe(member.name) or member.issym() or member.islnk():
                        raise ValueError(f"unsafe entry in archive: {member.name}")
                t.extractall(dest)
    else:
        raise ValueError(f"unsupported archive type {kind}")


def is_junk(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in JUNK_DIRS for p in parts) or parts[-1] in JUNK_FILES or parts[-1].startswith("._")


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""


def survey(root: Path) -> dict:
    """What installing root would put on disk: audio file count, total bytes, formats, and what's skipped."""
    files, total, formats, skipped = 0, 0, set(), []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in JUNK_DIRS]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if is_junk(rel):
                continue
            ext = ext_of(name)
            if ext not in AUDIO and ext not in EXTRAS:
                skipped.append(rel)
                continue
            total += os.path.getsize(os.path.join(dirpath, name))
            if ext in AUDIO:
                files += 1
                formats.add(ext)
    return {"files": files, "unpackedSize": total, "formats": sorted(formats), "skipped": sorted(skipped)}


def suggest_root(top: Path) -> str | None:
    """The single wrapper folder an archive often has (e.g. "Pack-1.0/"), so it isn't installed twice-nested."""
    root, rel = top, []
    while True:
        entries = [e for e in root.iterdir() if not is_junk(e.name) and e.name not in JUNK_DIRS]
        if len(entries) != 1 or not entries[0].is_dir():
            break
        root = entries[0]
        rel.append(root.name)
    return "/".join(rel) or None


def expand_asset(asset: dict, work: Path) -> Path:
    """Download + check + unpack an asset. Returns the folder Nota would install."""
    path = download(asset["url"])
    size = path.stat().st_size
    if size != asset["size"]:
        raise ValueError(f"size {size} != manifest {asset['size']}")
    digest = sha256(path)
    if digest != asset["sha256"]:
        raise ValueError(f"sha256 {digest} != manifest {asset['sha256']}")
    top = work / "x"
    unpack(path, archive_kind(asset), top)
    root = top / asset["root"] if asset.get("root") else top
    if not root.is_dir():
        raise ValueError(f"root '{asset.get('root')}' is not a folder in the archive (suggested: {suggest_root(top)})")
    return root


def cmd_verify(args) -> int:
    manifests = {m["id"]: m for _, m in load_manifests()}
    ids = args.ids or sorted(manifests)
    failures = 0
    for pid in ids:
        m = manifests.get(pid)
        if m is None:
            print(f"error: no pack '{pid}'")
            failures += 1
            continue
        latest = m["versions"][0]
        asset = latest["asset"]
        label = f"{pid} {latest['version']}"
        with tempfile.TemporaryDirectory(prefix="nota-verify-") as tmp:
            try:
                s = survey(expand_asset(asset, Path(tmp)))
                diff = [f"{k} {s[k]} != manifest {asset[k]}" for k in ("files", "unpackedSize", "formats") if s[k] != asset[k]]
                if diff:
                    raise ValueError("; ".join(diff))
                print(f"ok    {label}" + (f" ({len(s['skipped'])} non-sample file(s) skipped)" if s["skipped"] else ""))
            except Exception as e:  # noqa: BLE001 — report and carry on
                print(f"FAIL  {label}: {e}")
                failures += 1
            finally:
                # CI runners have little disk: don't keep big archives around.
                if os.environ.get("CI"):
                    shutil.rmtree(CACHE / hashlib.sha1(asset["url"].encode()).hexdigest()[:12], ignore_errors=True)
    print(f"{failures} failure(s)")
    return 1 if failures else 0


def cmd_inspect(args) -> int:
    src = args.source
    local = Path(src).exists()
    path = Path(src) if local else download(src)
    kind = args.archive or archive_kind_of(path.name)
    with tempfile.TemporaryDirectory(prefix="nota-inspect-") as tmp:
        top = Path(tmp) / "x"
        unpack(path, kind, top)
        root_rel = args.root if args.root is not None else suggest_root(top)
        s = survey(top / root_rel if root_rel else top)
    asset = {"url": src if not local else "https://…", "sha256": sha256(path), "size": path.stat().st_size}
    if kind != archive_kind_of(asset["url"]):
        asset["archive"] = kind
    if root_rel:
        asset["root"] = root_rel
    asset.update(files=s["files"], unpackedSize=s["unpackedSize"], formats=s["formats"])
    print(json.dumps({"asset": asset}, indent=2))
    if s["skipped"]:
        print(f"note: {len(s['skipped'])} file(s) Nota won't install, e.g. {s['skipped'][:5]}", file=sys.stderr)
    return 0


def cmd_outdated(_args) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    for _, m in load_manifests():
        match = GH_RELEASE_RE.match(m["versions"][0]["asset"]["url"])
        if not match:
            continue
        req = urllib.request.Request(f"https://api.github.com/repos/{match.group(1)}/releases/latest",
                                     headers={"User-Agent": UA, **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tag = json.load(r).get("tag_name", "")
        except Exception as e:  # noqa: BLE001
            print(f"?     {m['id']}: {e}")
            continue
        ours = m["versions"][0]["version"]
        state = "ok   " if tag.lstrip("vV") == ours.lstrip("vV") else "NEWER"
        print(f"{state} {m['id']}: registry {ours}, upstream {tag}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    b = sub.add_parser("build")
    b.add_argument("--out", default=str(ROOT / "dist"))
    b.set_defaults(fn=cmd_build)
    v = sub.add_parser("verify")
    v.add_argument("ids", nargs="*")
    v.set_defaults(fn=cmd_verify)
    i = sub.add_parser("inspect")
    i.add_argument("source", help="archive URL or local file")
    i.add_argument("--archive", choices=sorted(ARCHIVES))
    i.add_argument("--root", help="folder inside the archive to install (default: its single wrapper folder, if any)")
    i.set_defaults(fn=cmd_inspect)
    sub.add_parser("outdated").set_defaults(fn=cmd_outdated)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
