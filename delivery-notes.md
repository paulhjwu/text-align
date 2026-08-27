# Delivery notes — pre-deployment file checklist

Scope: what needs to be manually placed on the deployment server **before** the
container build runs there. Everything tracked in git (`src/`, `configs/`,
`data/`, `docs/`, `pyproject.toml`, `poetry.lock`, …) already arrives via
`git clone`/`git pull` and needs no separate action — this file only covers
what's gitignored but still required at build or run time.

All paths below are relative to the repo root; commands assume that's the
working directory.

## 1. Secrets — `.env`

Not in git (see `.env.example` for the full variable list). Provision through
your deployment's secrets mechanism — do not commit it, and do not let it pass
through a general file-copy step that might land in a build log or image
layer.

Only the variables for the provider(s) actually referenced by `llm_provider` /
`retry_llm_provider` in the `configs/*.yaml` files you're deploying are
required:

| Provider | Required env var(s) |
|---|---|
| openai | `OPENAI_API_KEY` |
| anthropic | `ANTHROPIC_API_KEY` |
| google | `GEMINI_API_KEY` |
| openrouter | `OPENROUTER_API_KEY` |
| gloo | `GLOO_CLIENT_ID`, `GLOO_CLIENT_SECRET` |
| ollama | `OLLAMA_BASE_URL` (only if not the localhost default) |

`configs/CUV.yaml` currently uses `gloo` for both the refine and retry passes,
so `GLOO_CLIENT_ID` / `GLOO_CLIENT_SECRET` are the minimum needed for that
config specifically.

## 2. ACAI entity data — `BibleAquifer/ACAI/`

Not in git (gitignored). Only needed for ACAI entity highlighting in
`render-alignment` — currently wired up via `acai_data_dir: ./BibleAquifer/ACAI`
in `configs/CUV.yaml`. Skip this if the container never runs `render-alignment`
for a config that sets `acai_data_dir`.

Preferred: clone fresh on the deployment server rather than copying — it's a
small public repo, no reason to ship a possibly-stale local copy:

```bash
git clone https://github.com/BibleAquifer/ACAI BibleAquifer/ACAI
```

Must land at `<repo-root>/BibleAquifer/ACAI` to match the config as written;
adjust `acai_data_dir` if the build context differs.

## 3. fhl_isa reference data + native module

Not in git (gitignored: `fhl_isa/node_modules/`, `fhl_isa/*.db`,
`fhl_isa/*.zip`). Only needed for locally-baked Greek/Hebrew morphology in the
render click-popup (`fhl_parsing_dir: ./fhl_isa` in `configs/CUV.yaml`) — if
skipped, the popup falls back to a live `bible.fhl.net` call per word, so this
is a functionality trade-off, not a build blocker.

**Copy these two files** (they're identical content to the `.db` files, just
compressed — copy the smaller zips and unzip on the server instead of shipping
the `.db` files directly):

| File | Size | Action on server |
|---|---|---|
| `fhl_isa/bible_parsing.zip` | 40 MB | copy → `unzip -o bible_parsing.zip` (produces 130 MB `bible_parsing.db`) |
| `fhl_isa/bible_little.zip` | 7.6 MB | copy → `unzip -o bible_little.zip` (produces 21 MB `bible_little.db`) |

**Do NOT copy `fhl_isa/node_modules/`.** It contains `better-sqlite3`'s
prebuilt native binding, which is ABI-specific to the exact Node version/OS/
architecture it was built against — copying it across environments is exactly
what broke local rendering here (a Node 19 vs Node 22 mismatch made the
binding fatal-crash on every DB open, silently, with `render-alignment` just
falling back to 0 local matches). Instead, run as part of the container build:

```bash
cd fhl_isa && npm install
```

This requires **Node ≥22** in the container image (`better-sqlite3@13.x`'s
stated engine requirement) — add it to the Dockerfile if not already present.

**Action needed:** `configs/CUV.yaml`'s `fhl_node_bin` is currently pinned to
an absolute path on this dev machine's `nvm` install
(`/home/paulhjwu/.nvm/versions/node/v22.22.2/bin/node`), which won't exist in
the container. Once the image has Node ≥22 available as the default `node` on
`PATH`, either delete that `fhl_node_bin` line (it defaults to bare `"node"`)
or override it for the deployment environment — don't ship the dev-machine
path as-is.

## 4. Everything else

No other gitignored path is required to build or run the container — `jobs/`,
`output/`, `staging/`, `scratch/` are ephemeral/local-only working directories,
not build inputs.

## Pre-flight checklist

- [ ] `.env` provisioned with the required provider keys for the configs being deployed
- [ ] `BibleAquifer/ACAI/` cloned at repo root (if any deployed config sets `acai_data_dir`)
- [ ] `fhl_isa/bible_parsing.zip` + `fhl_isa/bible_little.zip` copied and unzipped (if any deployed config sets `fhl_parsing_dir`)
- [ ] Container image has Node ≥22 on `PATH`
- [ ] `npm install` run inside `fhl_isa/` during the image build
- [ ] `configs/CUV.yaml`'s `fhl_node_bin` updated/removed for the target environment
- [ ] `poetry install` (or equivalent) run for the Python dependencies
