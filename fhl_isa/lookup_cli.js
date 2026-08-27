'use strict';

// CLI wrapper for bibleParsingLookup.js used by text_align's render-alignment
// (src/text_align/render/html.py, `_load_fhl_parsing`) to bake local, offline
// Greek/Hebrew morphology into the rendered HTML's click-popup.
//
// Reads a JSON array of words from stdin, looks each one up with
// getParsingInfo() (word-text match, accent/niqqud-insensitive), and writes
// a JSON object word -> decoded parsing (or null if no match) to stdout.
//
// A word can have multiple homonym occurrences across the whole Bible with
// different parsing (getParsingInfo isn't scoped to a specific verse - see
// bibleParsingLookup.js's own docstring). For each word this picks the
// decoded form - {strongNumber, line1, line2} - with the most matching
// occurrences, and within that group the most common gloss.

const { getParsingInfo } = require('./bibleParsingLookup.js');

function decodeMostCommon(word) {
  let results;
  try {
    results = getParsingInfo(word);
  } catch (err) {
    return null;
  }
  if (!results.length) return null;

  const groups = new Map(); // key -> { count, sample, glosses: Map }
  for (const r of results) {
    const key = JSON.stringify([
      r.strongNumber,
      r.parsedForm ? r.parsedForm.line1 : null,
      r.parsedForm ? r.parsedForm.line2 : null,
    ]);
    let g = groups.get(key);
    if (!g) {
      g = { count: 0, sample: r, glosses: new Map() };
      groups.set(key, g);
    }
    g.count += 1;
    g.glosses.set(r.gloss, (g.glosses.get(r.gloss) || 0) + 1);
  }

  let best = null;
  for (const g of groups.values()) {
    if (!best || g.count > best.count) best = g;
  }

  let bestGloss = best.sample.gloss;
  let bestGlossCount = -1;
  for (const [gloss, count] of best.glosses) {
    if (count > bestGlossCount) {
      bestGloss = gloss;
      bestGlossCount = count;
    }
  }

  const sample = best.sample;
  const parsedForm = sample.parsedForm;
  return {
    strongNumber: sample.strongNumber,
    partOfSpeech: parsedForm ? parsedForm.partOfSpeech : sample.partOfSpeech,
    partOfSpeechAbbr: parsedForm ? parsedForm.partOfSpeechAbbr : null,
    line1: parsedForm ? parsedForm.line1 : null,
    line2: parsedForm ? parsedForm.line2 : null,
    gloss: bestGloss,
    lemma: sample.lemma,
    transliteratedLemma: sample.transliteratedLemma,
    occurrences: results.length,
  };
}

function main() {
  const chunks = [];
  process.stdin.on('data', (c) => chunks.push(c));
  process.stdin.on('end', () => {
    let words;
    try {
      words = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch (err) {
      process.stderr.write(`Invalid JSON input: ${err.message}\n`);
      process.exit(1);
      return;
    }
    const out = {};
    for (const word of words) {
      out[word] = decodeMostCommon(word);
    }
    process.stdout.write(JSON.stringify(out));
  });
}

main();
