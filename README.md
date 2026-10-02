# Nota sample registry

The list of free sample packs that [Nota](https://github.com/nota-daw/nota) can download and install
into your Samples folder. Each pack is one JSON manifest in [`packs/`](packs). CI turns them into a single
`index.json`, published on GitHub Pages:

    https://nota-daw.github.io/nota-samples-registry/index.json

Nota downloads only this file. It then fetches the chosen pack straight from where its author publishes
it and checks the size and sha256 against the manifest before unpacking it.

## What gets in

- **Free for any music.** CC0, CC-BY or CC-BY-SA (or Unlicense/MIT), so the samples can be used in
  anything, commercial releases included. No NC/ND licenses and no "free for personal use". CC-BY packs
  carry an `attribution` line that Nota shows.
- **A stable download.** A GitHub release asset, a GitHub repo archive pinned to a tag or commit, or an
  archive.org file. Packs behind an email form, a login (Freesound originals) or a link that changes
  aren't listed.
- **Pinned.** Every pack has its exact `size` and `sha256`, plus what it unpacks to (`files`,
  `unpackedSize`, `formats`). A re-uploaded archive fails verification until the manifest is updated.
- **Samples Nota can load.** wav, aiff, flac, mp3 or ogg. Nota installs those plus docs and mappings
  (txt, md, pdf, sfz, mid, images); everything else in the archive (synth presets, Kontakt files, DAW
  projects) is skipped.
- **Recorded or synthesized by the author.** No rips of other libraries, hardware ROMs or records.

## Manifest

```jsonc
{
  "id": "cowsynth",                     // = file name; lowercase, digits, dashes
  "name": "Cowsynth",
  "author": "Karoryfer Samples",
  "description": "…",                   // ≤ 200 chars, shown in Nota
  "homepage": "https://…",              // optional
  "source": "https://github.com/sfzinstruments/karoryfer.cowsynth",  // where the author publishes it
  "license": "CC0-1.0",                 // CC0-1.0 | CC-BY-3.0 | CC-BY-4.0 | CC-BY-SA-3.0 | CC-BY-SA-4.0 | Unlicense | MIT
  "attribution": "…",                   // required for CC-BY*/MIT (≤ 200 chars)
  "kind": "multisample",                // one-shots | loops | multisample | mixed
  "tags": ["synth"],
  "notes": "…",                         // optional caveat shown in Nota (≤ 200 chars)
  "preview": "https://…/demo.ogg",      // optional audio demo (GitHub release/raw at a commit, archive.org)
  "bpm": 120, "key": "Am",              // optional, for loop packs
  "versions": [                         // newest first; Nota installs versions[0]
    {
      "version": "1.001",
      "asset": {
        "url": "https://github.com/…/releases/download/v1.001/Karoryfer.Cowsynth.v1.001.zip",
        "sha256": "…",
        "size": 14072509,
        "archive": "zip",               // optional; inferred from the URL (zip | tar)
        "root": "Cowsynth",             // optional: folder inside the archive to install
        "files": 40,                    // audio files Nota installs
        "unpackedSize": 14832612,       // bytes Nota installs (audio + docs)
        "formats": ["wav"]
      }
    }
  ]
}
```

Nota installs a pack into `<Samples folder>/Downloaded/<name>`, so it shows up in the browser's Files tab.

## Adding a pack or a version

From a GitHub repository, `add.py` drafts the manifest from the newest stable release (or from the repo
archive at a commit, for repos that are the pack itself), or prepends a new version to an existing
manifest. It needs a GitHub token, from `GITHUB_TOKEN` or a logged-in `gh`.

```sh
python3 scripts/add.py sfzinstruments/karoryfer.cowsynth --author "Karoryfer Samples" --tags synth
python3 scripts/add.py stargatedaw/stargate-sample-pack --commit --kind mixed
```

Descriptions and names are copied from the repo, so give them an edit before the PR. Keep them short,
plain and in sentence case. Packs that were checked and left out, and why, are in
[CANDIDATES.md](CANDIDATES.md).

By hand (archive.org, or any archive): `inspect` downloads it and prints a draft `asset` block with the
size, sha256, suggested `root` and contents:

```sh
python3 scripts/registry.py inspect https://archive.org/download/…/Pack.zip
```

Write the manifest, then:

```sh
python3 scripts/registry.py validate          # static checks
python3 scripts/registry.py verify cowsynth   # download, hash, unpack, compare contents
```

Open a PR. CI validates everything and verifies the packs you touched. Merging to `main` republishes
`index.json`. A weekly job re-verifies every pack and reports GitHub-hosted packs with a newer release
(`python3 scripts/registry.py outdated`).

## Testing Nota against a local index

```sh
python3 scripts/registry.py build --out dist
NOTA_SAMPLE_REGISTRY="$PWD/dist/index.json" <run Nota>
```
