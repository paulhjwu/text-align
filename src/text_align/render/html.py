"""render-alignment: generate per-chapter HTML alignment visualizations.

Produces one HTML file per chapter.  Each verse is rendered as a row of
inline-block cells in translation order.  Each cell has two rows:

  TOP:    target (translation) token text
  BOTTOM: source (Greek/Hebrew) anchor text, or a relationship symbol

Symbols follow the SBL Reverse Interlinear convention:
  →  / ←   Non-anchor token whose Greek source is shown in another cell
  ▶N        Token separated from its group by intervening tokens (group N)
  •         English word with no Greek source (ellipsis / redundancy)
  ≠         Token confirmed to have no equivalent in the other language (NEQ)
  ‹ … ›    Multiple Greek words behind a single English word/phrase

Idiomatic records and non-idiom records with multiple primary targets collapse
their primary tokens into merged cells: the anchor run (longest, rightmost for
LTR) shows joined text and source tokens; non-anchor runs show joined text with
a triangle+number pointer (▶N) back to the anchor.
If the target tokens are discontiguous, the longest contiguous run is the
anchor; remaining runs show triangle+number pointers.
ACAI entity tokens are highlighted.
NEQ tokens (source or target) are rendered in grey; all other symbols use
the default text color.

Clicking a Greek/Hebrew source word (``.grk`` spans) opens a popup with its
morphology.  When ``--fhl-parsing-dir`` points at a checkout of the
``fhl_isa`` tool (``bibleParsingLookup.js`` + ``bible_parsing.db``), the
popup is filled in at render time from that local, offline database
(part of speech, tense/voice/mood, case/number/gender, gloss, lemma) —
looked up by word text via ``fhl_isa/lookup_cli.js``, one batched Node
subprocess call per corpus.  Words with no local match, or when
``--fhl-parsing-dir`` is omitted, fall back to the popup's original
behavior: a live fetch of the Strong's-number dictionary entry from
bible.fhl.net.

``--flip`` additionally writes one ``<chapter>-flip.html`` per chapter, with
the source/target roles swapped: cells are ordered by the verse's own source
(Greek/Hebrew) index rather than translation order, TOP holds the source
word(s) and BOTTOM the translation, and every correspondence type is
distinguished visually — Primary (plain), Secondary (`.sec`, italic, no
separate source word), NEQ (`.neq`, grey ``≠``, a positive claim of no
correspondence), and Idiom (`.idiom`, italic, phrase-to-phrase merged into
one cell). A legend is written at the top of each flip file. See
``write_verse_flip`` / ``_precompute_flip_cells``.

CLI entry point: ``render-alignment``
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import regex as re
from biblelib.word import BCVID, BCVWPID

from text_align.burrito import AlignmentSet, Manager
from text_align.burrito.alignments import AlignmentsReader
from text_align.config import load_config_from_args, require
from text_align.languages import RTL_LANGUAGES
from text_align.align.acai_common import (
    ACAI_TYPES,
    AcaiEntity,
    build_word_entity_map,
    load_acai_entities,
)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """\
<style>
body  { font-family: serif; font-size: 14px; }
.cell { display: inline-block; vertical-align: top; padding: 1px 4px;
        text-align: center; min-width: 1.2em; }
.tgt  { display: block; margin-bottom: 2px; min-height: 1.2em; }
.src  { display: block; font-size: 90%; }
.neq  { color: #bbb; }
.idiom{ font-style: italic; }
.sec  { font-style: italic; color: #557; }

.legend { font-family: Arial, sans-serif; font-size: 12px; color: #444;
          margin: 0 0 14px 0; padding: 8px 10px; background: #f6f6f6;
          border-radius: 4px; border: 1px solid #e2e2e2; }
.legend b { color: #222; }
.legend .legend-item { margin-right: 18px; white-space: nowrap; }
.legend .sample { padding: 0 2px; }

.sub  { font-size: 60%; }
.tri  { font-size: 90%; vertical-align: 1px; }
.acai-hl  { background: #d0e8ff; border-radius: 2px; padding: 0 1px; }
.acai-tag { font-size: 55%; font-family: Arial, sans-serif;
            text-transform: uppercase; position: relative; top: -0.6em;
            margin-left: 0.4em; color: #446; }
.file-meta { font-family: Arial, sans-serif; font-size: 12px; color: #666;
             margin: 2px 0 14px 0; letter-spacing: 0.01em; }
.file-meta .meta-edition { font-weight: bold; color: #333; }
.file-meta .meta-sep { color: #bbb; margin: 0 7px; }

.grk  { cursor: pointer; border-bottom: 1px dotted #77a; }
.grk:hover { background: #eef4ff; }
#strongs-popup { position: absolute; max-width: 360px; max-height: 320px; overflow: auto;
    background: #fff; border: 1px solid #99a; border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.25); padding: 8px 10px 8px 10px;
    font-family: Arial, sans-serif; font-size: 12px; line-height: 1.4;
    text-align: left; z-index: 1000; }
#strongs-popup-close { position: absolute; top: 2px; right: 6px; cursor: pointer;
    color: #888; font-weight: bold; padding: 0 4px; }
.strongs-rec { margin-bottom: 6px; padding-bottom: 6px; border-bottom: 1px solid #eee; }
.strongs-rec:last-child { margin-bottom: 0; padding-bottom: 0; border-bottom: none; }
.strongs-row { margin: 1px 0; }
.strongs-label { font-weight: bold; color: #446; }
</style>
"""

# ---------------------------------------------------------------------------
# Strong's-definition popup (fhl.net) — click a Greek/Hebrew source word to
# fetch and display its Strong's-number definition inline.
# ---------------------------------------------------------------------------

_JS = """\
<script>
window.addEventListener('DOMContentLoaded', function () {
  // FHL's own testament flag is the opposite of what "isGreek" would suggest:
  // N=0 is the NT/Greek dictionary, N=1 is the OT/Hebrew dictionary.
  async function getStrongsDefinition(sn, N) {
    const url = `https://bible.fhl.net/json/sd.php?N=${N}&k=${sn}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
    return await response.json();
  }

  function parseStrongsCode(code) {
    const m = /^([GH])0*(\\d+)([a-d])?$/.exec(code || '');
    if (!m) return null;
    return { N: m[1] === 'G' ? 0 : 1, k: m[2] + (m[3] || '') };
  }

  function renderParsing(container, parsed) {
    container.innerHTML = '';
    const block = document.createElement('div');
    block.className = 'strongs-rec';
    const rows = [
      ["Strong's", parsed.strongNumber],
      ['Part of speech', parsed.partOfSpeech],
      ['Form', [parsed.line1, parsed.line2].filter(Boolean).join(', ')],
      ['Lemma', parsed.lemma],
      ['Gloss', parsed.gloss],
    ];
    for (const [label, value] of rows) {
      if (!value) continue;
      const row = document.createElement('div');
      row.className = 'strongs-row';
      const lab = document.createElement('span');
      lab.className = 'strongs-label';
      lab.textContent = label + ': ';
      row.appendChild(lab);
      const val = document.createElement('span');
      val.textContent = value;
      row.appendChild(val);
      block.appendChild(row);
    }
    container.appendChild(block);
  }

  function renderDefinition(container, data) {
    container.innerHTML = '';
    const records = Array.isArray(data)
      ? data
      : (data && Array.isArray(data.record) ? data.record : (data ? [data] : []));
    if (!records.length) {
      container.textContent = 'No definition found.';
      return;
    }
    for (const rec of records) {
      const block = document.createElement('div');
      block.className = 'strongs-rec';
      for (const [key, value] of Object.entries(rec)) {
        if (value === null || value === undefined || value === '') continue;
        const row = document.createElement('div');
        row.className = 'strongs-row';
        const label = document.createElement('span');
        label.className = 'strongs-label';
        label.textContent = key + ': ';
        row.appendChild(label);
        const val = document.createElement('span');
        val.innerHTML = String(value);
        row.appendChild(val);
        block.appendChild(row);
      }
      container.appendChild(block);
    }
  }

  const popup = document.createElement('div');
  popup.id = 'strongs-popup';
  popup.style.display = 'none';
  popup.innerHTML = "<span id='strongs-popup-close'>&times;</span><div id='strongs-popup-body'></div>";
  document.body.appendChild(popup);
  const closeBtn = popup.querySelector('#strongs-popup-close');
  const body = popup.querySelector('#strongs-popup-body');
  closeBtn.addEventListener('click', function () { popup.style.display = 'none'; });
  document.addEventListener('click', function (e) {
    if (popup.style.display !== 'none' && !popup.contains(e.target) && !e.target.classList.contains('grk')) {
      popup.style.display = 'none';
    }
  });

  document.addEventListener('click', async function (e) {
    const el = e.target.closest('.grk');
    if (!el) return;
    e.stopPropagation();
    const rect = el.getBoundingClientRect();
    popup.style.left = (window.scrollX + rect.left) + 'px';
    popup.style.top = (window.scrollY + rect.bottom + 4) + 'px';
    popup.style.display = 'block';

    // Local morphology, baked in at render time by --fhl-parsing-dir, takes
    // priority over the live fhl.net fetch — no network round-trip needed.
    if (el.dataset.fhl) {
      try {
        renderParsing(body, JSON.parse(el.dataset.fhl));
      } catch (err) {
        body.textContent = `Failed to parse local morphology data: ${err.message}`;
      }
      return;
    }

    const code = el.dataset.strongs;
    const parsed = parseStrongsCode(code);
    if (!parsed) {
      body.textContent = "No morphology or Strong's data available for this word.";
      return;
    }
    body.textContent = `Loading ${code} ...`;
    try {
      const data = await getStrongsDefinition(parsed.k, parsed.N);
      renderDefinition(body, data);
    } catch (err) {
      body.textContent = `Failed to fetch definition for ${code}: ${err.message}`;
    }
  });
});
</script>
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AlignmentToken:
    """All data needed to render one alignment record as verse cells."""

    targets: dict[str, str]                              # tid → text
    sources: dict[str, str]                              # sid → text
    source_strongs: dict[str, str] = field(default_factory=dict)  # sid → Strong's code
    primary_targets: frozenset[str] = field(default_factory=frozenset)
    secondary_targets: frozenset[str] = field(default_factory=frozenset)
    separated_targets: frozenset[str] = field(default_factory=frozenset)
    is_idiom: bool = False
    is_neq_src: bool = False
    is_neq_tgt: bool = False


# ---------------------------------------------------------------------------
# Source/target text lookup helpers
# ---------------------------------------------------------------------------

def get_source_text(source_id: str, sources: list) -> str:
    for src in sources:
        if src.id == source_id:
            return src.text
    print(f"Could not find source for {source_id}")
    return ""


def get_alignment_sources(source_selectors: list[str], sources: list) -> dict[str, str]:
    return {sel: get_source_text(sel, sources) for sel in sorted(source_selectors)}


def get_alignment_source_strongs(source_selectors: list[str], sources: list) -> dict[str, str]:
    """Return sid → Strong's code for each source selector that has one."""
    strong_by_id = {src.id: src.strong for src in sources if src.strong}
    return {sel: strong_by_id[sel] for sel in source_selectors if sel in strong_by_id}


def get_alignment_targets(target_selectors: list[str], targets: list) -> dict[str, str]:
    return {t.id: t.text for t in targets if t.id in target_selectors}


def get_sources_with_targets(records: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for bcv_records in records.values():
        for alignment in bcv_records:
            for source_id in alignment.source_selectors:
                result[source_id] = alignment.target_selectors
    return result


def _load_fhl_parsing(
    words: set[str], fhl_dir: Path, node_bin: str = "node"
) -> dict[str, dict]:
    """Batch-lookup Greek/Hebrew morphology for `words` via fhl_isa's
    bibleParsingLookup.js (local bible_parsing.db, no network access).

    Runs `fhl_dir/lookup_cli.js` once for the whole word set: each word is
    matched by text (accent/niqqud-insensitive, per bibleParsingLookup.js's
    `getParsingInfo`), and the decoded form with the most matching
    occurrences is kept — a word can have multiple homonym occurrences
    across the whole Bible with different parsing, and this isn't tied to
    the specific verse being rendered (bible_parsing.db keys rows by a text
    book abbreviation, not the BCVWPID scheme used elsewhere in this file).

    Returns word -> decoded parsing dict; words with no local match are
    omitted.  Returns {} (after printing a warning) if Node, the script, or
    the database is unavailable, so rendering proceeds without local
    morphology and each `.grk` span falls back to its Strong's-code popup.
    """
    if not words:
        return {}
    script = (fhl_dir / "lookup_cli.js").resolve()
    try:
        proc = subprocess.run(
            [node_bin, str(script)],
            input=json.dumps(sorted(words)),
            cwd=fhl_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else exc
        print(f"  fhl_isa parsing lookup failed ({detail}); continuing without local morphology")
        return {}
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"  fhl_isa parsing lookup returned invalid JSON ({exc}); continuing without local morphology")
        return {}
    return {word: info for word, info in raw.items() if info}


def get_unused_verse_sources(
    verse_tids: list[str],
    alignments: dict[str, list[AlignmentToken]],
    verse_sources: list,
) -> dict[str, str]:
    used: set[str] = set()
    for tid in verse_tids:
        for tok in alignments.get(tid, []):
            used.update(tok.sources.keys())
    return {src.id: src.text for src in verse_sources if src.id not in used}


# ---------------------------------------------------------------------------
# Cell rendering helpers
# ---------------------------------------------------------------------------

def _word_index(word_id: str, ref_id: str) -> str:
    """Return the subscript index string for `word_id`, positioned relative
    to `ref_id` (same verse / same chapter / elsewhere). Generic over any
    BCVWPID-shaped id pair — used for source-relative-to-target indices in
    the normal view and target-relative-to-source indices in `--flip` mode.
    """
    src_bcv = BCVWPID(word_id)
    tgt_bcv = BCVWPID(ref_id)
    is_nt = int(src_bcv.book_ID) > 39
    same_verse = src_bcv.to_bcvid == tgt_bcv.to_bcvid
    same_chapter = src_bcv.chapter_ID == tgt_bcv.chapter_ID
    if is_nt:
        w = int(src_bcv.word_ID)
        if same_verse:
            return str(w)
        elif same_chapter:
            return f"{int(src_bcv.verse_ID)}.{w}"
        else:
            return f"{int(src_bcv.chapter_ID)}:{int(src_bcv.verse_ID)}.{w}"
    else:
        w, p = int(src_bcv.word_ID), int(src_bcv.part_ID)
        if same_verse:
            return f"{w}.{p}"
        elif same_chapter:
            return f"{int(src_bcv.verse_ID)}.{w}.{p}"
        else:
            return f"{int(src_bcv.chapter_ID)}:{int(src_bcv.verse_ID)}.{w}.{p}"


def _attr_escape(s: str) -> str:
    """Escape a string for embedding inside a single-quoted HTML attribute."""
    return (
        s.replace("&", "&amp;")
        .replace("'", "&#39;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _grk_span(text: str, strong: str, parsing: dict | None = None) -> str:
    """Wrap source-word text in a clickable span carrying its popup data.

    `parsing`, when given, is the word's local fhl_isa morphology (see
    `_load_fhl_parsing`) and is embedded as `data-fhl` so the click-popup can
    render it without a network fetch.  `strong` (the Strong's code) is kept
    as `data-strongs` regardless, as the popup's fallback when there is no
    local match.
    """
    if not strong and not parsing:
        return text
    attrs = []
    if strong:
        attrs.append(f"data-strongs='{strong}'")
    if parsing:
        payload = _attr_escape(json.dumps(parsing, ensure_ascii=False))
        attrs.append(f"data-fhl='{payload}'")
    return f"<span class='grk' {' '.join(attrs)}>{text}</span>"


def _anchor(token: AlignmentToken, verse_tids: list[str], is_r2l: bool) -> str | None:
    """Return the target_id that should display the Greek/Hebrew source text."""
    if not token.sources or token.is_neq_tgt:
        return None
    candidates = token.primary_targets - token.separated_targets
    if not candidates:
        candidates = token.primary_targets
    if not candidates:
        candidates = frozenset(token.targets.keys())
    ordered = [t for t in verse_tids if t in candidates]
    if not ordered:
        return None
    return ordered[0] if is_r2l else ordered[-1]


def _tri_toward(token_pos: int, anchor_pos: int, is_r2l: bool) -> str:
    """Return a small triangle character pointing toward the anchor cell."""
    points_right = (token_pos < anchor_pos) if not is_r2l else (token_pos > anchor_pos)
    return "▸" if points_right else "◂"


def _render_cell(
    target_id: str,
    token: AlignmentToken,
    verse_tids: list[str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]],
    tag_acai: bool,
    word_parsing: dict[str, dict] | None = None,
) -> str:
    anchor = _anchor(token, verse_tids, is_r2l)
    is_anchor = target_id == anchor
    is_secondary = target_id in token.secondary_targets
    is_separated = target_id in token.separated_targets

    target_text = token.targets.get(target_id, "")

    # ── top row: target text ──────────────────────────────────────────────
    tgt_classes: list[str] = []
    if token.is_idiom:
        tgt_classes.append("idiom")
    if token.is_neq_tgt:
        tgt_classes.append("neq")

    if tag_acai and token.sources and not token.is_neq_tgt and not is_secondary:
        acai_hits = [ae for sid in token.sources for ae in acai_entities.get(sid, [])]
        if acai_hits:
            tgt_classes.append("acai-hl")
            tag_str = " ".join(ae.id for ae in acai_hits)
            tgt_inner = (
                f"{target_text}"
                f"<span class='acai-tag'>{tag_str}</span>"
            )
        else:
            tgt_inner = target_text
    else:
        tgt_inner = target_text

    cls_str = " ".join(["tgt"] + tgt_classes)
    tgt_row = f"<div class='{cls_str}'>{tgt_inner}</div>"

    # ── bottom row: source reference ─────────────────────────────────────
    arrow_to_anchor: str
    if anchor and anchor in verse_tids and target_id in verse_tids:
        ap = verse_tids.index(anchor)
        mp = verse_tids.index(target_id)
        arrow_to_anchor = ("→" if mp < ap else "←") if not is_r2l else ("←" if mp < ap else "→")
    else:
        arrow_to_anchor = "→"

    if is_anchor:
        parts: list[str] = []
        for sid in sorted(token.sources):
            idx = _word_index(sid, target_id)
            parsing = (word_parsing or {}).get(token.sources[sid])
            grk = _grk_span(token.sources[sid], token.source_strongs.get(sid, ""), parsing)
            parts.append(f"{grk}<sub class='sub'>{idx}</sub>")
        if len(parts) > 1:
            inner = "&nbsp;".join(parts)
            greek_html = f"‹{inner}›"
        else:
            greek_html = parts[0] if parts else ""
        src_row = f"<div class='src'>{greek_html}</div>"

    elif is_separated:
        anchor_tid = _anchor(token, verse_tids, is_r2l)
        if anchor_tid and token.sources:
            ref_src_id = sorted(token.sources.keys())[0]
            ref_idx = _word_index(ref_src_id, anchor_tid)
            ap = verse_tids.index(anchor_tid)
            mp = verse_tids.index(target_id)
            tri = _tri_toward(mp, ap, is_r2l)
            src_row = f"<div class='src'><span class='arr'><span class='tri'>{tri}</span><sub class='sub'>{ref_idx}</sub></span></div>"
        else:
            tri = "◂" if is_r2l else "▸"
            src_row = f"<div class='src'><span class='arr'><span class='tri'>{tri}</span></span></div>"

    elif not token.sources:
        # unaligned target token — check NEQ first before generic bullet
        if token.is_neq_tgt:
            src_row = "<div class='src'><span class='neq'>≠</span></div>"
        elif re.search(r"\w", target_text):
            src_row = "<div class='src'><span class='arr'>•</span></div>"
        else:
            src_row = "<div class='src'>&nbsp;</div>"

    else:
        # non-anchor (secondary adjacent or non-anchor primary)
        src_row = f"<div class='src'><span class='arr'>{arrow_to_anchor}</span></div>"

    return f"<div class='cell'>{tgt_row}{src_row}</div>"


def _contiguous_runs(
    ordered_tids: list[str],
    verse_tids: list[str],
) -> list[list[str]]:
    """Split ordered_tids into contiguous groups based on their positions in verse_tids."""
    if not ordered_tids:
        return []
    pos = {t: i for i, t in enumerate(verse_tids)}
    runs: list[list[str]] = []
    current = [ordered_tids[0]]
    for prev, curr in zip(ordered_tids, ordered_tids[1:]):
        if pos.get(curr, -1) == pos.get(prev, -2) + 1:
            current.append(curr)
        else:
            runs.append(current)
            current = [curr]
    runs.append(current)
    return runs


def _precompute_idiom_cells(
    token: AlignmentToken,
    verse_tids: list[str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]],
    tag_acai: bool,
    out: dict[str, tuple[str | None, list[str]]],
    word_parsing: dict[str, dict] | None = None,
) -> None:
    """Populate out[target_id] = (html | None, source_ids) for an idiom record.

    The anchor cell gets merged target text and all source tokens; other cells
    in the anchor run are set to (None, []) so they are skipped.  Tokens in
    non-anchor runs get arrow cells.
    """
    idiom_tids = [t for t in verse_tids if t in token.targets]
    if not idiom_tids:
        return

    runs = _contiguous_runs(idiom_tids, verse_tids)
    # Anchor run = longest; ties broken by rightmost for LTR, leftmost for RTL
    anchor_run = max(
        runs,
        key=lambda r: (len(r), verse_tids.index(r[-1]) if not is_r2l else -verse_tids.index(r[0])),
    )
    anchor_display = anchor_run[0] if is_r2l else anchor_run[-1]

    # ── anchor cell ───────────────────────────────────────────────────────
    combined_text = " ".join(token.targets.get(t, "") for t in anchor_run)
    tgt_inner = combined_text
    if tag_acai and token.sources:
        acai_hits = [ae for sid in token.sources for ae in acai_entities.get(sid, [])]
        if acai_hits:
            tag_str = " ".join(ae.id for ae in acai_hits)
            tgt_inner = (
                f"<span class='acai-hl'>{combined_text}</span>"
                f"<span class='acai-tag'>{tag_str}</span>"
            )
    tgt_row = f"<div class='tgt idiom'>{tgt_inner}</div>"

    if token.sources:
        parts: list[str] = []
        for sid in sorted(token.sources):
            idx = _word_index(sid, anchor_display)
            parsing = (word_parsing or {}).get(token.sources[sid])
            grk = _grk_span(token.sources[sid], token.source_strongs.get(sid, ""), parsing)
            parts.append(f"{grk}<sub class='sub'>{idx}</sub>")
        inner = "&nbsp;".join(parts)
        greek_html = f"‹{inner}›" if len(parts) > 1 else inner
        src_row = f"<div class='src'>{greek_html}</div>"
    else:
        src_row = "<div class='src'>&nbsp;</div>"

    out[anchor_display] = (f"<div class='cell'>{tgt_row}{src_row}</div>", list(token.sources.keys()))

    for t in anchor_run:
        if t != anchor_display:
            out[t] = (None, [])

    # ── triangle+number cells for non-anchor runs ────────────────────────
    ref_idx = _word_index(sorted(token.sources.keys())[0], anchor_display) if token.sources else ""
    anchor_pos = verse_tids.index(anchor_display)
    for run in runs:
        if run is anchor_run:
            continue
        run_display = run[0] if is_r2l else run[-1]
        tri = _tri_toward(verse_tids.index(run_display), anchor_pos, is_r2l)
        combined_r = " ".join(token.targets.get(t, "") for t in run)
        tgt_row_r = f"<div class='tgt idiom'>{combined_r}</div>"
        src_row_r = f"<div class='src'><span class='arr'><span class='tri'>{tri}</span><sub class='sub'>{ref_idx}</sub></span></div>"
        out[run_display] = (f"<div class='cell'>{tgt_row_r}{src_row_r}</div>", [])
        for t in run:
            if t != run_display:
                out[t] = (None, [])


def _precompute_multiprimary_cells(
    token: AlignmentToken,
    verse_tids: list[str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]],
    tag_acai: bool,
    out: dict[str, tuple[str | None, list[str]]],
    word_parsing: dict[str, dict] | None = None,
) -> None:
    """Populate out[target_id] for a non-idiom record with multiple primary targets.

    Primary tokens are grouped into contiguous runs.  The anchor run (longest,
    rightmost for LTR / leftmost for RTL) merges its tokens into one cell with
    the source text.  Non-anchor primary runs each merge into one cell with a
    triangle+number pointer.  Secondary tokens show an arrow if adjacent to the
    anchor, or a triangle+number pointer if separated from it by non-members.
    """
    pri_tids = [t for t in verse_tids if t in token.primary_targets]
    if not pri_tids:
        return

    pri_runs = _contiguous_runs(pri_tids, verse_tids)

    anchor_run = max(
        pri_runs,
        key=lambda r: (len(r), verse_tids.index(r[-1]) if not is_r2l else -verse_tids.index(r[0])),
    )
    anchor_display = anchor_run[0] if is_r2l else anchor_run[-1]
    anchor_pos = verse_tids.index(anchor_display)
    ref_idx = _word_index(sorted(token.sources.keys())[0], anchor_display) if token.sources else ""

    # ── anchor run ───────────────────────────────────────────────────────────
    combined = " ".join(token.targets.get(t, "") for t in anchor_run)
    tgt_inner = combined
    if tag_acai and token.sources:
        acai_hits = [ae for sid in token.sources for ae in acai_entities.get(sid, [])]
        if acai_hits:
            tag_str = " ".join(ae.id for ae in acai_hits)
            tgt_inner = (
                f"<span class='acai-hl'>{combined}</span>"
                f"<span class='acai-tag'>{tag_str}</span>"
            )
    tgt_row = f"<div class='tgt'>{tgt_inner}</div>"

    if token.sources:
        parts = [
            f"{_grk_span(token.sources[sid], token.source_strongs.get(sid, ''), (word_parsing or {}).get(token.sources[sid]))}"
            f"<sub class='sub'>{_word_index(sid, anchor_display)}</sub>"
            for sid in sorted(token.sources)
        ]
        inner = "&nbsp;".join(parts)
        greek_html = f"‹{inner}›" if len(parts) > 1 else inner
        src_row = f"<div class='src'>{greek_html}</div>"
    else:
        src_row = "<div class='src'>&nbsp;</div>"

    out[anchor_display] = (f"<div class='cell'>{tgt_row}{src_row}</div>", list(token.sources.keys()))
    for t in anchor_run:
        if t != anchor_display:
            out[t] = (None, [])

    # ── non-anchor primary runs ───────────────────────────────────────────────
    for run in pri_runs:
        if run is anchor_run:
            continue
        run_display = run[0] if is_r2l else run[-1]
        tri = _tri_toward(verse_tids.index(run_display), anchor_pos, is_r2l)
        combined_r = " ".join(token.targets.get(t, "") for t in run)
        tgt_row_r = f"<div class='tgt'>{combined_r}</div>"
        src_row_r = f"<div class='src'><span class='arr'><span class='tri'>{tri}</span><sub class='sub'>{ref_idx}</sub></span></div>"
        out[run_display] = (f"<div class='cell'>{tgt_row_r}{src_row_r}</div>", [])
        for t in run:
            if t != run_display:
                out[t] = (None, [])

    # ── secondary tokens ─────────────────────────────────────────────────────
    pos = {t: i for i, t in enumerate(verse_tids)}
    sec_tids = [t for t in verse_tids if t in token.secondary_targets]

    for t in sec_tids:
        t_pos = pos.get(t, -1)
        if t_pos < 0:
            continue
        tgt_text = token.targets.get(t, "")
        lo, hi = min(t_pos, anchor_pos), max(t_pos, anchor_pos)
        has_gap = any(verse_tids[p] not in token.targets for p in range(lo + 1, hi))

        if has_gap:
            tri = _tri_toward(t_pos, anchor_pos, is_r2l)
            src_row_s = f"<div class='src'><span class='arr'><span class='tri'>{tri}</span><sub class='sub'>{ref_idx}</sub></span></div>"
        else:
            ap = verse_tids.index(anchor_display)
            mp = verse_tids.index(t)
            arrow = ("→" if mp < ap else "←") if not is_r2l else ("←" if mp < ap else "→")
            src_row_s = f"<div class='src'><span class='arr'>{arrow}</span></div>"

        out[t] = (f"<div class='cell'><div class='tgt'>{tgt_text}</div>{src_row_s}</div>", [])


# ---------------------------------------------------------------------------
# --flip rendering: source word on top (verse's own source order), its
# correspondence(s) below. Mirrors the cell-building helpers above with the
# source/target axes swapped: CSS classes are unchanged ('.tgt' = top row,
# '.src' = bottom row, 90% size), but in flip mode '.tgt' holds the source
# word and '.src' holds the translation, so the anchor headword keeps the
# larger top-row styling regardless of which language it is.
# ---------------------------------------------------------------------------

_FLIP_LEGEND = (
    "<div class='legend'>"
    "<span class='legend-item'><b>Primary</b> <span class='sample'>word</span> "
    "— direct lexical/semantic link</span>"
    "<span class='legend-item'><b>Secondary</b> <span class='sample sec'>word</span> "
    "— grammatically implied, no separate source word</span>"
    "<span class='legend-item'><b>NEQ</b> <span class='sample neq'>≠</span> "
    "— positive claim of no correspondence</span>"
    "<span class='legend-item'><b>Idiom</b> <span class='sample idiom'>phrase</span> "
    "— phrase-to-phrase, treated as one unit</span>"
    "</div>\n"
)


def _precompute_flip_cells(
    token: AlignmentToken,
    verse_source_ids: list[str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]],
    tag_acai: bool,
    out: dict[str, tuple[str | None, list[str]]],
    word_parsing: dict[str, dict] | None = None,
) -> None:
    """Populate out[source_id] = (html | None, target_ids) for one alignment
    record in --flip (source-anchored) mode.

    Groups the record's source ids into contiguous runs (within
    verse_source_ids order); the anchor run (longest, rightmost for LTR /
    leftmost for RTL — same tie-break as the target-anchored helpers above)
    renders as one merged cell: TOP = joined source word(s), clickable and
    subscript-indexed when there is more than one; BOTTOM = every target word
    the record covers, each individually marked primary (plain) or secondary
    (`.sec`, italic) — with the whole row additionally marked `.idiom` or
    `.neq` when the record is flagged as such. Non-anchor runs (a record's
    source ids split by intervening, unrelated source tokens) render as
    pointer cells (triangle + index) back to the anchor.
    """
    rec_src_ids = [s for s in verse_source_ids if s in token.sources]
    if not rec_src_ids:
        return

    runs = _contiguous_runs(rec_src_ids, verse_source_ids)
    anchor_run = max(
        runs,
        key=lambda r: (len(r), verse_source_ids.index(r[-1]) if not is_r2l else -verse_source_ids.index(r[0])),
    )
    anchor_display = anchor_run[0] if is_r2l else anchor_run[-1]

    # ── bottom row: every target word, primary/secondary distinguished,
    # each subscript-indexed by its own position in the translation ────────
    ordered_tids = sorted(token.targets)
    tgt_parts = []
    for t in ordered_tids:
        text = token.targets[t]
        idx = _word_index(t, anchor_display)
        word_html = f"<span class='sec'>{text}</span>" if t in token.secondary_targets else text
        tgt_parts.append(f"{word_html}<sub class='sub'>{idx}</sub>")
    combined_tgt = "&nbsp;".join(tgt_parts)
    if len(tgt_parts) > 1:
        combined_tgt = f"‹{combined_tgt}›"

    translation_classes = ["src"]
    if token.is_idiom:
        translation_classes.append("idiom")
    if token.is_neq_tgt:
        translation_classes.append("neq")
    translation_row = f"<div class='{' '.join(translation_classes)}'>{combined_tgt}</div>"

    # ── top row: anchor run's source word(s) ────────────────────────────────
    src_parts = []
    for sid in anchor_run:
        idx = _word_index(sid, anchor_display)
        parsing = (word_parsing or {}).get(token.sources[sid])
        grk = _grk_span(token.sources[sid], token.source_strongs.get(sid, ""), parsing)
        src_parts.append(f"{grk}<sub class='sub'>{idx}</sub>")
    source_inner = "&nbsp;".join(src_parts)
    if len(src_parts) > 1:
        source_inner = f"‹{source_inner}›"

    if tag_acai and not token.is_neq_tgt:
        acai_hits = [ae for sid in anchor_run for ae in acai_entities.get(sid, [])]
        if acai_hits:
            tag_str = " ".join(ae.id for ae in acai_hits)
            source_inner = f"<span class='acai-hl'>{source_inner}</span><span class='acai-tag'>{tag_str}</span>"
    source_row = f"<div class='tgt'>{source_inner}</div>"

    out[anchor_display] = (f"<div class='cell'>{source_row}{translation_row}</div>", list(token.targets.keys()))
    for s in anchor_run:
        if s != anchor_display:
            out[s] = (None, [])

    # ── non-anchor runs: pointer cells back to the anchor ───────────────────
    ref_idx = _word_index(anchor_run[0], anchor_display)
    anchor_pos = verse_source_ids.index(anchor_display)
    for run in runs:
        if run is anchor_run:
            continue
        run_display = run[0] if is_r2l else run[-1]
        tri = _tri_toward(verse_source_ids.index(run_display), anchor_pos, is_r2l)
        combined_r = " ".join(token.sources.get(s, "") for s in run)
        source_row_r = f"<div class='tgt'>{combined_r}</div>"
        translation_row_r = f"<div class='src'><span class='arr'><span class='tri'>{tri}</span><sub class='sub'>{ref_idx}</sub></span></div>"
        out[run_display] = (f"<div class='cell'>{source_row_r}{translation_row_r}</div>", [])
        for s in run:
            if s != run_display:
                out[s] = (None, [])


def write_verse_flip(
    html_out,
    verse_tids: list[str],
    alignments: dict[str, list[AlignmentToken]],
    combined_sources: list,
    neq_source: frozenset[str],
    neq_target: frozenset[str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]],
    sources_with_targets: dict,
    tag_acai: bool,
    word_parsing: dict[str, dict] | None = None,
) -> None:
    """--flip mode: source word on top (verse's own source order), its
    correspondence(s) below. Mirrors write_verse with source and target roles
    swapped; see _precompute_flip_cells for the per-record cell logic.
    """
    verse_source_ids = sorted(src.id for src in combined_sources)
    unused_source_ids = get_unused_verse_sources(verse_tids, alignments, combined_sources) if combined_sources else {}

    cell_map: dict[str, tuple[str | None, list[str]]] = {}
    seen_ids: set[int] = set()
    for target_id in verse_tids:
        tok_list = alignments.get(target_id, [])
        if not tok_list:
            continue
        tok = tok_list[0]
        if not tok.sources or id(tok) in seen_ids:
            continue
        seen_ids.add(id(tok))
        _precompute_flip_cells(tok, verse_source_ids, is_r2l, acai_entities, tag_acai, cell_map, word_parsing)

    cells: list[dict] = []  # {"html": str, "target_ids": list[str]}
    for source_id in verse_source_ids:
        if source_id in cell_map:
            cell_html, tgt_ids = cell_map[source_id]
            if cell_html is None:
                continue  # absorbed into merged anchor cell
            cells.append({"html": cell_html, "target_ids": tgt_ids})
        elif source_id in unused_source_ids and source_id not in sources_with_targets:
            # source token with no alignment record at all — either NEQ (positive
            # claim of no correspondence) or simply unaligned.
            source_text = unused_source_ids[source_id]
            is_neq = source_id in neq_source
            marker = "≠" if is_neq else "•"
            cls = " class='neq'" if is_neq else ""
            idx = _word_index(source_id, source_id)
            parsing = (word_parsing or {}).get(source_text)
            grk = _grk_span(source_text, "", parsing)
            source_row = f"<div class='tgt'><span{cls}>{grk}<sub class='sub'>{idx}</sub></span></div>"
            translation_row = f"<div class='src'><span{cls}>{marker}</span></div>"
            cells.append({"html": f"<div class='cell'>{source_row}{translation_row}</div>", "target_ids": []})

    # ── orphan targets: no source at all (unaligned or NEQ target) ─────────
    # These have no natural position in source order, so insert each one
    # next to the translation-cell of its nearest positioned neighbor by
    # target id — the mirror of write_verse's unused-source insertion.
    orphan_tids: list[str] = []
    for tid in verse_tids:
        tok_list = alignments.get(tid, [])
        if tok_list and not tok_list[0].sources:
            orphan_tids.append(tid)

    for tid in orphan_tids:
        tok = alignments[tid][0]
        tgt_text = tok.targets.get(tid, "")
        is_neq = tok.is_neq_tgt or tid in neq_target
        if is_neq:
            source_row = "<div class='tgt'><span class='neq'>≠</span></div>"
            text_cls = " class='neq'"
        elif re.search(r"\w", tgt_text):
            source_row = "<div class='tgt'><span class='arr'>•</span></div>"
            text_cls = ""
        else:
            source_row = "<div class='tgt'>&nbsp;</div>"
            text_cls = ""
        idx = _word_index(tid, tid)
        translation_row = f"<div class='src'><span{text_cls}>{tgt_text}<sub class='sub'>{idx}</sub></span></div>"
        orphan_cell = {"html": f"<div class='cell'>{source_row}{translation_row}</div>", "target_ids": [tid]}

        try:
            orphan_int = int(tid)
        except (ValueError, TypeError):
            orphan_int = None

        best_after_cell = None
        best_after_int = -1
        best_before_cell = None
        best_before_int: int | None = None

        for cell in cells:
            for t in cell["target_ids"]:
                try:
                    t_int = int(t)
                except (ValueError, TypeError):
                    continue
                if orphan_int is not None:
                    if t_int < orphan_int and t_int > best_after_int:
                        best_after_int = t_int
                        best_after_cell = cell
                    elif t_int > orphan_int and (best_before_int is None or t_int < best_before_int):
                        best_before_int = t_int
                        best_before_cell = cell

        if best_after_cell is not None:
            after_idx = cells.index(best_after_cell)
            after_tgt_set = set(best_after_cell["target_ids"])
            while after_idx + 1 < len(cells):
                if after_tgt_set & set(cells[after_idx + 1]["target_ids"]):
                    after_idx += 1
                else:
                    break
            cells.insert(after_idx + 1, orphan_cell)
        elif best_before_cell is not None:
            cells.insert(cells.index(best_before_cell), orphan_cell)
        else:
            cells.append(orphan_cell)

    for cell in cells:
        html_out.write(cell["html"])


# ---------------------------------------------------------------------------
# Verse rendering
# ---------------------------------------------------------------------------

def write_verse(
    html_out,
    verse_tids: list[str],
    alignments: dict[str, list[AlignmentToken]],
    unused_source_ids: dict[str, str],
    neq_source: frozenset[str],
    is_r2l: bool,
    acai_entities: dict[str, list[AcaiEntity]],
    sources_with_targets: dict,
    tag_acai: bool,
    tgt_verse_bcvid: str = "",
    word_parsing: dict[str, dict] | None = None,
) -> None:
    cells: list[dict] = []  # {"html": str, "source_ids": list[str]}

    # Pre-compute merged cells for idiom and multi-primary records
    idiom_cell_map: dict[str, tuple[str | None, list[str]]] = {}
    multiprimary_cell_map: dict[str, tuple[str | None, list[str]]] = {}
    seen_idiom_ids: set[int] = set()
    seen_multiprimary_ids: set[int] = set()
    for target_id in verse_tids:
        tok_list = alignments.get(target_id, [])
        if not tok_list:
            continue
        tok = tok_list[0]
        if tok.is_idiom and id(tok) not in seen_idiom_ids:
            seen_idiom_ids.add(id(tok))
            _precompute_idiom_cells(tok, verse_tids, is_r2l, acai_entities, tag_acai, idiom_cell_map, word_parsing)
        elif not tok.is_idiom and len(tok.primary_targets) > 1 and id(tok) not in seen_multiprimary_ids:
            seen_multiprimary_ids.add(id(tok))
            _precompute_multiprimary_cells(tok, verse_tids, is_r2l, acai_entities, tag_acai, multiprimary_cell_map, word_parsing)

    for target_id in verse_tids:
        tok_list = alignments.get(target_id, [])
        if not tok_list:
            continue
        tok = tok_list[0]
        if tok.is_idiom:
            if target_id not in idiom_cell_map:
                continue
            cell_html, src_ids = idiom_cell_map[target_id]
            if cell_html is None:
                continue  # absorbed into merged anchor cell
            cells.append({"html": cell_html, "source_ids": src_ids})
        elif not tok.is_idiom and len(tok.primary_targets) > 1:
            if target_id not in multiprimary_cell_map:
                continue
            cell_html, src_ids = multiprimary_cell_map[target_id]
            if cell_html is None:
                continue  # absorbed into merged anchor cell
            cells.append({"html": cell_html, "source_ids": src_ids})
        else:
            cell_html = _render_cell(target_id, tok, verse_tids, is_r2l, acai_entities, tag_acai, word_parsing)
            cells.append({"html": cell_html, "source_ids": list(tok.sources.keys())})

    # insert unaligned / NEQ source tokens in positional order
    for unused_id in sorted(unused_source_ids, reverse=True):
        if unused_id in sources_with_targets:
            continue
        source_text = unused_source_ids[unused_id]
        src_bcv = BCVWPID(unused_id)
        is_nt = int(src_bcv.book_ID) > 39
        same_verse = src_bcv.to_bcvid == tgt_verse_bcvid
        same_chapter = src_bcv.chapter_ID == tgt_verse_bcvid[2:5]
        if is_nt:
            w = int(src_bcv.word_ID)
            if same_verse:
                idx_str = str(w)
            elif same_chapter:
                idx_str = f"{int(src_bcv.verse_ID)}.{w}"
            else:
                idx_str = f"{int(src_bcv.chapter_ID)}:{int(src_bcv.verse_ID)}.{w}"
        else:
            w, p = int(src_bcv.word_ID), int(src_bcv.part_ID)
            if same_verse:
                idx_str = f"{w}.{p}"
            elif same_chapter:
                idx_str = f"{int(src_bcv.verse_ID)}.{w}.{p}"
            else:
                idx_str = f"{int(src_bcv.chapter_ID)}:{int(src_bcv.verse_ID)}.{w}.{p}"

        is_neq = unused_id in neq_source
        marker = "≠" if is_neq else "•"
        src_cls = " class='neq'" if is_neq else ""
        tgt_cls = " class='neq'" if is_neq else ""

        tgt_row = f"<div class='tgt'><span{tgt_cls}>{marker}</span></div>"
        parsing = (word_parsing or {}).get(source_text)
        grk = _grk_span(source_text, "", parsing)
        src_row = (
            f"<div class='src'><span{src_cls}>"
            f"{grk}<sub class='sub'>{idx_str}</sub></span></div>"
        )
        src_cell = {"html": f"<div class='cell'>{tgt_row}{src_row}</div>", "source_ids": [unused_id]}

        # Determine insertion point by source-token ordering:
        #   Rule 1: NEQ before all source cells → prepend.
        #   Rule 2: NEQ after all source cells → append.
        #   Rule 3: insert after the cell whose source is the highest value
        #           still less than the NEQ — i.e., right after the preceding
        #           Greek token's translation cell (the natural reading position).
        #   Rule 4: fallback — insert before the nearest following source cell.
        try:
            neq_int = int(unused_id)
        except (ValueError, AttributeError):
            neq_int = None

        best_after_cell = None   # cell for nearest preceding Greek token (rule 3)
        best_after_int = -1
        best_before_cell = None  # cell for nearest following Greek token (rule 4)
        best_before_int: int | None = None

        for cell in cells:
            for sid in cell["source_ids"]:
                try:
                    sid_int = int(sid)
                    if neq_int is not None:
                        if sid_int < neq_int and sid_int > best_after_int:
                            best_after_int = sid_int
                            best_after_cell = cell
                        elif sid_int > neq_int and (best_before_int is None or sid_int < best_before_int):
                            best_before_int = sid_int
                            best_before_cell = cell
                except (ValueError, AttributeError):
                    pass

        if best_after_cell is not None:
            # Rule 3: after the nearest preceding source token's translation cell.
            # Advance past any immediately following cells that belong to the same
            # alignment unit (same source IDs) so we never split a contiguous group.
            after_idx = cells.index(best_after_cell)
            after_src_set = set(best_after_cell["source_ids"])
            while after_idx + 1 < len(cells):
                if after_src_set & set(cells[after_idx + 1]["source_ids"]):
                    after_idx += 1
                else:
                    break
            cells.insert(after_idx + 1, src_cell)
        elif best_before_cell is not None:
            # Rule 1: NEQ precedes all positioned tokens — before the first
            cells.insert(cells.index(best_before_cell), src_cell)
        else:
            # Rule 2 / no positioned cells yet: append
            cells.append(src_cell)

    for cell in cells:
        html_out.write(cell["html"])


# ---------------------------------------------------------------------------
# HTML structure helpers
# ---------------------------------------------------------------------------

def _html_open(is_r2l: bool) -> str:
    if is_r2l:
        return (
            "<html dir='rtl'>\n<head>\n<meta charset=\"utf-8\">\n"
            + _CSS
            + _JS
            + "<style>body { direction: rtl; }</style>\n"
        )
    return "<html>\n<head>\n<meta charset=\"utf-8\">\n" + _CSS + _JS


def _build_meta_row(meta_info: dict) -> str:
    """Build the styled metadata row shown below the chapter heading."""
    parts: list[str] = []

    edition = meta_info.get("translation_abbr", "")
    name = meta_info.get("translation_name", "")
    if edition and name:
        parts.append(f"<span class='meta-edition'>{edition}</span> — {name}")
    elif edition:
        parts.append(f"<span class='meta-edition'>{edition}</span>")
    elif name:
        parts.append(name)

    def _llm_str(llm: dict, label: str = "") -> str:
        provider = llm.get("provider", "")
        model = llm.get("model", "")
        effort = llm.get("reasoning_effort", "")
        if not (provider or model):
            return ""
        s = f"{provider} / {model}" if provider and model else model
        if effort:
            s += f" effort:{effort}"
        return f"{label}{s}" if label else s

    llm = meta_info.get("llm") or {}
    retry_llm = meta_info.get("retry_llm") or {}
    if retry_llm:
        s = _llm_str(llm, "Refined: ")
        if s:
            parts.append(s)
        s = _llm_str(retry_llm, "Retried: ")
        if s:
            parts.append(s)
    else:
        s = _llm_str(llm)
        if s:
            parts.append(s)

    iso_date = meta_info.get("iso_date", "")
    if iso_date:
        parts.append(iso_date)

    if not parts:
        return ""
    sep = "<span class='meta-sep'>·</span>"
    return f"<div class='file-meta'>{sep.join(parts)}</div>\n"


def start_new_chapter(
    html_out, bcv: BCVWPID, viz_path: Path, is_r2l: bool, iso_date: str,
    meta_info: dict | None = None, suffix: str = "", legend_html: str = "",
) -> object:
    if html_out is not None:
        html_out.close()
    chapter_file = viz_path / f"{bcv.book_ID}-{bcv.chapter_ID}{suffix}.html"
    html_out = open(chapter_file, "w", encoding="utf-8")
    usfm_ref = re.sub(r":[0-9]+$", "", bcv.to_usfm())
    html_out.write(_html_open(is_r2l))
    html_out.write(
        f"<title>{usfm_ref} ({bcv.book_ID}-{bcv.chapter_ID}{suffix})</title>\n</head>\n<body>\n"
    )
    html_out.write(f"<h1>{usfm_ref}</h1>\n")
    effective_meta = dict(meta_info or {})
    effective_meta.setdefault("iso_date", iso_date)
    html_out.write(_build_meta_row(effective_meta))
    if legend_html:
        html_out.write(legend_html)
    html_out.write("<div class='chapter'>\n")
    html_out = start_new_verse(html_out, bcv, is_r2l)
    return html_out


def start_new_verse(html_out, bcv: BCVWPID, is_r2l: bool) -> object:
    usfm = bcv.to_usfm()
    dir_attr = " dir='rtl'" if is_r2l else ""
    html_out.write(f"<p style='display: block'{dir_attr}><b>{usfm}:</b>&nbsp;\n")
    return html_out


def end_verse(html_out) -> None:
    html_out.write("\n</p><!-- verse -->\n")


def end_chapter(html_out, level: str = "chapter") -> None:
    end_verse(html_out)
    html_out.write(f"</div><!-- {level} -->\n</body></html>\n")


# ---------------------------------------------------------------------------
# Discontiguous detection
# ---------------------------------------------------------------------------

def _detect_discontiguous(
    alignments: dict[str, list[AlignmentToken]],
    is_r2l: bool,
) -> None:
    """Assign separated_targets to discontiguous AlignmentTokens in-place."""
    # group all target IDs by verse
    verse_to_tids: dict[str, list[str]] = {}
    for tid in sorted(alignments.keys()):
        bcvid = BCVWPID(tid).to_bcvid
        verse_to_tids.setdefault(bcvid, []).append(tid)

    seen: set[int] = set()

    for verse_tids in verse_to_tids.values():
        pos = {t: i for i, t in enumerate(verse_tids)}
        for tid in verse_tids:
            for token in alignments.get(tid, []):
                if id(token) in seen:
                    continue
                seen.add(id(token))

                if len(token.targets) < 2:
                    continue

                rec_tids = [t for t in verse_tids if t in token.targets]
                if len(rec_tids) < 2:
                    continue

                positions = sorted(pos[t] for t in rec_tids)
                has_gap = any(
                    verse_tids[p] not in token.targets
                    for i in range(len(positions) - 1)
                    for p in range(positions[i] + 1, positions[i + 1])
                )
                if not has_gap:
                    continue

                # anchor position (rightmost primary for LTR, leftmost for RTL)
                pri = [t for t in rec_tids if t in token.primary_targets]
                if not pri:
                    pri = rec_tids[:1]
                anchor_pos = (min if is_r2l else max)(pos[t] for t in pri)

                separated: set[str] = set()
                for t in rec_tids:
                    if t in token.primary_targets:
                        continue
                    t_pos = pos[t]
                    lo, hi = min(t_pos, anchor_pos), max(t_pos, anchor_pos)
                    if any(verse_tids[p] not in token.targets for p in range(lo + 1, hi)):
                        separated.add(t)

                token.separated_targets = frozenset(separated)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    config_defaults = load_config_from_args(output_suffix="viz", output_in_exp=False)

    p = argparse.ArgumentParser(
        description="Render alignment data as per-chapter HTML visualizations."
    )
    p.add_argument("--config", metavar="NAME",
                   help="Load defaults from configs/<NAME>.yaml (CLI args override)")
    p.add_argument("--alignment-lang", default=None,
                   help="ISO 639-3 language code for the target translation, e.g. spa")
    p.add_argument("--alignment-edition", default=None,
                   help="Target edition ID, e.g. BONBV")
    p.add_argument("--lang-data-path", default=None, type=Path,
                   help="Root data/ directory for the target language alignment repo")
    p.add_argument("--output-dir", default=None, type=Path,
                   help="Root directory to write HTML output files")
    p.add_argument("--alignment-dir", default=None, type=Path,
                   help="Directory containing alignment JSON files to render "
                        "(overrides the standard alignments/ path derived from "
                        "--lang-data-path; e.g. exp/OENGB/LLM-REFINED)")
    p.add_argument("--acai-data-dir", default=None, type=Path,
                   help="Path to ACAI root directory (omit to disable ACAI annotations)")
    p.add_argument("--acai-types", nargs="+", default=ACAI_TYPES,
                   help=f"ACAI entity types to load (default: {ACAI_TYPES})")
    p.add_argument("--include-acai-pronominals", action="store_true",
                   help="Include pronominal referents in ACAI entity data")
    p.add_argument("--r2l", action=argparse.BooleanOptionalAction, default=None,
                   help="Target language is right-to-left. Auto-detected from "
                        "--alignment-lang when omitted; pass --r2l/--no-r2l to override.")
    p.add_argument("--target-edition-name", default=None,
                   help="Full translation name shown in the HTML header (e.g. 'Biblia de Nuestra Familia')")
    p.add_argument("--fhl-parsing-dir", default=None, type=Path,
                   help="Directory containing fhl_isa's bibleParsingLookup.js, "
                        "lookup_cli.js, and bible_parsing.db. When given, the "
                        "click-popup on Greek/Hebrew source words is filled in "
                        "at render time with local morphology (offline, no "
                        "fhl.net fetch) for any word found in that database.")
    p.add_argument("--fhl-node-bin", default="node",
                   help="Node executable used to run fhl_isa's lookup script (default: node)")
    p.add_argument("--flip", action="store_true",
                   help="Additionally write a source-anchored '<chapter>-flip.html' per "
                        "chapter: source word on top (sorted by source index), its "
                        "correspondence(s) — primary, secondary, NEQ, idiom — below.")
    p.set_defaults(**config_defaults)
    args = p.parse_args()
    require(args, "alignment_lang", "alignment_edition", "lang_data_path", "output_dir")
    return args


def main() -> None:
    args = parse_args()
    iso_date = datetime.datetime.now().isoformat().split("T")[0]
    is_r2l = args.r2l if args.r2l is not None else args.alignment_lang in RTL_LANGUAGES
    tag_acai = args.acai_data_dir is not None

    print("Loading AlignmentSets ...")
    adir = args.alignment_dir
    managers = []
    for sourceid, canon in (("WLCM", "ot"), ("SBLGNT", "nt")):
        try:
            # Prefer chapter files ({sourceid}-{edition}-BB-CCC-manual.json) when present
            chapter_files = (
                sorted(adir.glob(f"{sourceid}-{args.alignment_edition}-??-???-manual.json"))
                if adir else []
            )
            if chapter_files:
                print(f"  Found {len(chapter_files)} chapter file(s) for {sourceid} — merging")
                # Use first chapter file as the alignmentpath sentinel (exists, assertion passes)
                alset = AlignmentSet(
                    targetlanguage=args.alignment_lang,
                    targetid=args.alignment_edition,
                    sourceid=sourceid,
                    langdatapath=args.lang_data_path,
                    alignmentpath_override=chapter_files[0],
                )
                reader = AlignmentsReader.from_chapter_files(chapter_files, alset)
                managers.append(Manager(alset, preloaded_reader=reader))
            else:
                override = adir / f"{sourceid}-{args.alignment_edition}-manual.json" if adir else None
                alset = AlignmentSet(
                    targetlanguage=args.alignment_lang,
                    targetid=args.alignment_edition,
                    sourceid=sourceid,
                    langdatapath=args.lang_data_path,
                    alignmentpath_override=override,
                )
                managers.append(Manager(alset))
        except (AssertionError, FileNotFoundError) as exc:
            print(f"Skipping {canon.upper()} ({sourceid}): {exc}")

    for mgr in managers:
        corpus = "ot" if mgr.alignmentset.sourceid == "WLCM" else "nt"
        print(f"\nRendering {corpus.upper()} - {mgr.alignmentset.sourceid}")

        acai_word_map: dict[str, list[AcaiEntity]] = {}
        if tag_acai and args.acai_data_dir is not None:
            raw_entities = load_acai_entities(
                args.acai_data_dir, args.acai_types, corpus,
                include_pronominals=args.include_acai_pronominals,
            )
            acai_word_map = build_word_entity_map(raw_entities)
            print(f"  ACAI word map: {len(acai_word_map)} entries")

        neq_source = mgr.alignmentsreader.neq_source
        neq_target = mgr.alignmentsreader.neq_target
        sources_with_targets = get_sources_with_targets(mgr.bcv["records"])

        word_parsing: dict[str, dict] = {}
        if args.fhl_parsing_dir:
            all_source_words = {
                src.text for src_list in mgr.bcv["sources"].values() for src in src_list
            }
            print(f"  Looking up local fhl_isa morphology for {len(all_source_words)} distinct word(s) ...")
            word_parsing = _load_fhl_parsing(all_source_words, args.fhl_parsing_dir, args.fhl_node_bin)
            print(f"  fhl_isa matched {len(word_parsing)}/{len(all_source_words)} word(s)")

        # ── build verse-mapping dicts for merged-verse support ──────────
        # _src_to_tgt_verse: source verse BCV → target verse BCV, derived
        # from alignment records.  Needed for record processing (line below)
        # and to augment _tgt_to_src_vids with cross-boundary source verses.
        # Example: SBLGNT 3JN 1:15 records target BSB 1:14, so
        # _src_to_tgt_verse["63014015"] = "63014014".
        _src_to_tgt_verse: dict[str, str] = {}
        for _rec_id, _rec_list in mgr.bcv["records"].items():
            for _alignment in _rec_list:
                if _alignment.target_selectors:
                    _src_to_tgt_verse[_rec_id] = BCVWPID(_alignment.target_selectors[0]).to_bcvid
                    break

        # _tgt_to_src_vids: target verse BCV → ordered list of source verse BCVs.
        # Step 1: derive from target token source_verse field (authoritative).
        # This correctly handles OT versification mismatches: BSB 2:1 tokens
        # carry source_verse="32002002" (Hebrew 2:2), so Hebrew 2:1 never bleeds
        # into the chapter 2 rendering even when it is present in the source corpus.
        _tgt_to_src_vids: dict[str, list[str]] = {}
        for _tgt in mgr.targetitems.values():
            _src_bcv = _tgt.source_verse
            _tgt_bcv = _tgt.bcv
            _svids = _tgt_to_src_vids.setdefault(_tgt_bcv, [])
            if _src_bcv not in _svids:
                _svids.append(_src_bcv)
        # Step 2: augment from alignment records for source verses that cross
        # target verse boundaries (e.g. SBLGNT 3JN 1:15 merged into BSB 1:14).
        # NT target tokens only carry their own verse as source_verse, so the
        # extra source verse would otherwise be invisible.
        for _sv, _tv in _src_to_tgt_verse.items():
            _svids = _tgt_to_src_vids.setdefault(_tv, [])
            if _sv not in _svids:
                _svids.append(_sv)

        _tgt_combined_sources: dict[str, list] = {
            _tv: sorted(
                [t for _sv in _svs for t in mgr.bcv["sources"].get(_sv, [])],
                key=lambda t: t.id,
            )
            for _tv, _svs in _tgt_to_src_vids.items()
        }

        # ── build AlignmentToken dict ────────────────────────────────────
        alignments: dict[str, list[AlignmentToken]] = {}

        for record_id, record in mgr.bcv["records"].items():
            tgt_vid = _src_to_tgt_verse.get(record_id, record_id)
            sources = _tgt_combined_sources.get(tgt_vid, mgr.bcv["sources"].get(record_id, []))
            targets_source = mgr.bcv["target_sourceverses"].get(record_id)
            if targets_source is None and tgt_vid != record_id:
                targets_source = mgr.bcv["target_sourceverses"].get(tgt_vid)
            if targets_source is None:
                print(f"  No target_sourceverses for {record_id}, skipping")
                continue

            for alignment in record:
                al_sources = get_alignment_sources(alignment.source_selectors, sources)
                al_source_strongs = get_alignment_source_strongs(alignment.source_selectors, sources)
                al_targets = get_alignment_targets(alignment.target_selectors, targets_source)
                if not al_targets and alignment.target_selectors:
                    # Fallback for tokens whose source_verse in the TSV defaults to their
                    # own BCV (e.g. title verse tokens with no source_verse set), making
                    # them invisible to target_sourceverses[record_id].
                    al_targets = {
                        tid: mgr.targetitems[tid].text
                        for tid in alignment.target_selectors
                        if tid in mgr.targetitems
                    }
                if not al_targets:
                    continue

                _secondary = alignment.meta.secondary
                if not isinstance(_secondary, dict):
                    _secondary = {}
                sec_tgts = frozenset(_secondary.get("target", []))
                pri_tgts = frozenset(al_targets.keys()) - sec_tgts
                token = AlignmentToken(
                    targets=al_targets,
                    sources=al_sources,
                    source_strongs=al_source_strongs,
                    primary_targets=pri_tgts,
                    secondary_targets=sec_tgts,
                    is_idiom=alignment.meta.is_idiom,
                    is_neq_src=bool(set(al_sources.keys()) & neq_source),
                    is_neq_tgt=bool(set(al_targets.keys()) & neq_target),
                )
                for tid in al_targets:
                    alignments.setdefault(tid, []).append(token)

        # Unaligned / NEQ-only targets — run as a second pass after ALL alignment
        # records have been processed.  Running this inside the records loop caused
        # a shadowing bug for merged verses (e.g. BSB 3JN 1:14 = SBLGNT 1:14-15):
        # empty-source placeholders for the secondary-source-verse targets were
        # inserted before those records were processed, making the placeholder
        # tok_list[0] at render time even after the correct token was appended.
        #
        # Derive the set of target verse BCVs from the aligned tokens themselves —
        # more accurate than tracking one BCV per source verse (which breaks when
        # a single source verse covers multiple target verses, e.g. Hebrew Ps 130:1
        # covering both BSB 130:0 and 130:1).  Use mgr.bcv["targets"] (keyed by
        # the token's own BCV) rather than target_sourceverses (keyed by
        # source_verse), so tokens whose source_verse defaults to their own BCV
        # (e.g. title verse tokens with no source_verse in the TSV) are found.
        aligned_tgt_verse_bcvs = {BCVWPID(tid).to_bcvid for tid in alignments}
        for _tgt_vid in aligned_tgt_verse_bcvs:
            for target in mgr.bcv["targets"].get(_tgt_vid, []):
                if target.id not in alignments:
                    alignments[target.id] = [AlignmentToken(
                        targets={target.id: target.text},
                        sources={},
                        primary_targets=frozenset({target.id}),
                        is_neq_tgt=target.id in neq_target,
                    )]

        # ── post-process: discontiguous groups ───────────────────────────
        _detect_discontiguous(alignments, is_r2l)

        target_ids = sorted(alignments)
        if not target_ids:
            print("  No targets, skipping.")
            continue

        # ── group target IDs by verse ────────────────────────────────────
        verse_to_tids: dict[str, list[str]] = {}
        for tid in target_ids:
            bcvid = BCVWPID(tid).to_bcvid
            verse_to_tids.setdefault(bcvid, []).append(tid)

        viz_path = (
            args.output_dir
            / f"{args.alignment_edition}/{mgr.alignmentset.sourceid}-{args.alignment_edition}"
        )
        viz_path.mkdir(parents=True, exist_ok=True)

        meta_info: dict = {
            "translation_abbr": args.alignment_edition,
            "translation_name": args.target_edition_name or "",
            "iso_date": iso_date,
            "llm": mgr.alignmentsreader.group_meta.get("llm") or {},
            "retry_llm": mgr.alignmentsreader.group_meta.get("retry_llm") or {},
        }

        html_out = None
        prev_chapter_key = ""

        for bcvid, verse_tids in verse_to_tids.items():
            current_bcv = BCVWPID(verse_tids[0])
            chapter_key = f"{current_bcv.book_ID}-{current_bcv.chapter_ID}"

            if chapter_key != prev_chapter_key:
                if html_out is not None:
                    end_chapter(html_out)
                # Use this chapter's own meta when available (mixed-provider directories)
                chapter_file_meta = mgr.alignmentsreader.per_chapter_meta.get(chapter_key)
                if chapter_file_meta:
                    meta_info["llm"] = chapter_file_meta.get("llm") or {}
                    meta_info["retry_llm"] = chapter_file_meta.get("retry_llm") or {}
                html_out = start_new_chapter(html_out, current_bcv, viz_path, is_r2l, iso_date, meta_info)
                prev_chapter_key = chapter_key
            else:
                end_verse(html_out)
                html_out = start_new_verse(html_out, current_bcv, is_r2l)

            src_bcvid = BCVWPID(verse_tids[0]).to_bcvid
            combined = _tgt_combined_sources.get(src_bcvid, mgr.bcv["sources"].get(src_bcvid, []))
            unused = get_unused_verse_sources(verse_tids, alignments, combined) if combined else {}

            write_verse(
                html_out, verse_tids, alignments, unused,
                neq_source, is_r2l, acai_word_map, sources_with_targets, tag_acai,
                tgt_verse_bcvid=src_bcvid, word_parsing=word_parsing,
            )

        if html_out is not None:
            end_chapter(html_out, "book")

        print(f"  HTML written to {viz_path}")

        if args.flip:
            html_out = None
            prev_chapter_key = ""

            for bcvid, verse_tids in verse_to_tids.items():
                current_bcv = BCVWPID(verse_tids[0])
                chapter_key = f"{current_bcv.book_ID}-{current_bcv.chapter_ID}"

                if chapter_key != prev_chapter_key:
                    if html_out is not None:
                        end_chapter(html_out)
                    chapter_file_meta = mgr.alignmentsreader.per_chapter_meta.get(chapter_key)
                    if chapter_file_meta:
                        meta_info["llm"] = chapter_file_meta.get("llm") or {}
                        meta_info["retry_llm"] = chapter_file_meta.get("retry_llm") or {}
                    html_out = start_new_chapter(
                        html_out, current_bcv, viz_path, is_r2l, iso_date, meta_info,
                        suffix="-flip", legend_html=_FLIP_LEGEND,
                    )
                    prev_chapter_key = chapter_key
                else:
                    end_verse(html_out)
                    html_out = start_new_verse(html_out, current_bcv, is_r2l)

                src_bcvid = BCVWPID(verse_tids[0]).to_bcvid
                combined = _tgt_combined_sources.get(src_bcvid, mgr.bcv["sources"].get(src_bcvid, []))

                write_verse_flip(
                    html_out, verse_tids, alignments, combined,
                    neq_source, neq_target, is_r2l, acai_word_map, sources_with_targets, tag_acai,
                    word_parsing=word_parsing,
                )

            if html_out is not None:
                end_chapter(html_out, "book")

            print(f"  Flip HTML written to {viz_path}")


if __name__ == "__main__":
    main()
