'use strict';

const assert = require('assert');
const { inspect } = require('util');
const { getParsingInfo, detectLanguage, normalizeWord } = require('./bibleParsingLookup.js');

const TOKEN = 'Ἰδὼν'; // aorist active participle, nsm, of ὁράω ("having seen")

function section(title) {
  console.log(`\n=== ${title} ===`);
}

const lang = detectLanguage(TOKEN);
assert.strictEqual(lang, 'greek', 'expected token to be detected as Greek');

const normalized = normalizeWord(TOKEN, 'greek');
assert.strictEqual(normalized, 'ιδων', 'expected accents/breathing to be stripped and lowercased');

const results = getParsingInfo(TOKEN);

const expCounts = new Map();
for (const r of results) {
  expCounts.set(r.gloss, (expCounts.get(r.gloss) || 0) + 1);
}
const collapsedRecord = {
  strongNumber: results[0] ? results[0].strongNumber : null,
  exp: Array.from(expCounts, ([value, count]) => ({ value, count })),
};

assert.ok(results.length > 0, 'expected at least one match for Ἰδὼν');
for (const r of results) {
  assert.strictEqual(r.strongNumber, '03708', `expected Strong's number 03708 (ὁράω), got ${r.strongNumber}`);
  assert.strictEqual(normalizeWord(r.inflectedWord, 'greek'), 'ιδων');
}

const exactResults = getParsingInfo(TOKEN, { exact: true });
// Greek oxytone words shift their accent from acute (´) to grave (`)
// depending on whether a pause/punctuation follows in the sentence, so the
// same lemma/form legitimately appears in the source text spelled both
// "Ἰδών" and "Ἰδὼν". Exact match on one spelling is therefore expected to
// be a strict subset of the accent-insensitive normalized match, not equal
// to it.
assert.ok(exactResults.length > 0, 'expected at least one exact match for the grave-accented spelling');
assert.ok(
  exactResults.length <= results.length,
  'exact match count should never exceed the normalized match count'
);
const normalizedIds = new Set(results.map((r) => `${r.book}|${r.chapter}|${r.verse}|${r.wordPosition}`));
for (const r of exactResults) {
  const key = `${r.book}|${r.chapter}|${r.verse}|${r.wordPosition}`;
  assert.ok(normalizedIds.has(key), `exact match ${key} should also appear in the normalized match set`);
}

const bareToken = 'ιδων'; // no capital, no breathing/accent
const bareResults = getParsingInfo(bareToken);
assert.strictEqual(bareResults.length, results.length, 'accent-free spelling should match the same rows as the fully-accented token');

let mismatchCount = 0;
try {
  const wrongAccent = getParsingInfo('ιδων', { exact: true }); // no accents, exact=true
  mismatchCount = wrongAccent.length;
} catch (e) {
  // exact mode throwing on an unaccented lookup is also an acceptable outcome.
}
assert.strictEqual(mismatchCount, 0, 'exact match without proper accents should not find the accented stored form');

const rawResults = getParsingInfo(TOKEN, { exact: true, raw: true });
assert.strictEqual(rawResults.length, exactResults.length, 'raw+exact should match the same row count as exact mode for this input');

section('decoded morphology (parsedForm) - reproduces the card shown for Ἰδὼν');
const decoded = { ...results[0].parsedForm, exp: collapsedRecord };
console.log(inspect(decoded, { depth: null }));
assert.strictEqual(decoded.partOfSpeech, 'Verb');
assert.strictEqual(decoded.line1, '2aor. act. ptc.', 'expected tense/voice/mood line to match the card\'s "aor. act. participle"');
assert.strictEqual(decoded.line2, 'nom. masc. sg.', 'expected case/gender/number line to match the card');
console.log('partOfSpeech:', decoded.partOfSpeech, `(${decoded.partOfSpeechAbbr})`);
console.log('line1:', decoded.line1, ' <- card shows "aor. act. participle"');
console.log('line2:', decoded.line2, ' <- card shows "nom. masc. sg."');
console.log(
  'note: card says "aor." (1st aorist); this token is actually the 2nd/strong aorist participle of ὁράω (stem ἰδ-), which this decoder distinguishes as "2aor." - grammatically both are just "aorist" in English, the decoder keeps the 1st/2nd distinction only because the source data encodes it separately.'
);
