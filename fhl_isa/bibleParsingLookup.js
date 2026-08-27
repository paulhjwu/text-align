'use strict';

const path = require('path');
const Database = require('better-sqlite3');

const DEFAULT_DB_PATH = path.join(__dirname, 'bible_parsing.db');

const HEBREW_RANGE = new RegExp('[\\u0590-\\u05FF]');
const GREEK_RANGE = new RegExp('[\\u0370-\\u03FF\\u1F00-\\u1FFF]');

// Hebrew niqqud (vowel points) + cantillation marks + the maqaf/meteg etc.
// These are combining marks that sit in the text between/after consonants,
// so they must be stripped, not decomposed, to get a bare consonant skeleton.
const HEBREW_POINTS = new RegExp('[\\u0591-\\u05C7]', 'g');

// Per-table in-memory index, built lazily on first lookup and reused after
// that. bible_parsing.db is a static reference dataset (~480k word rows
// total), so caching a normalized-word -> rows index is far cheaper than
// re-scanning or trying to express Unicode-aware normalization in SQL.
const tableIndexCache = new Map(); // key: `${dbPath}::${table}` -> { byWord, byLemma }

function detectLanguage(word) {
  if (HEBREW_RANGE.test(word)) return 'hebrew';
  if (GREEK_RANGE.test(word)) return 'greek';
  return null;
}

function stripHebrewPoints(word) {
  return word.replace(HEBREW_POINTS, '');
}

function stripGreekAccents(word) {
  return word
    .normalize('NFD')
    // combining accents/breathings/iota subscript (U+0300-036F combining
    // diacriticals, U+1DC0-1DFF combining diacriticals supplement)
    .replace(new RegExp('[\\u0300-\\u036F\\u1DC0-\\u1DFF]', 'g'), '')
    .normalize('NFC')
    .replace(new RegExp('\\u03C2', 'g'), 'σ') // treat final sigma (ς) as medial (σ)
    .toLowerCase();
}

// Reduces a word to a form suitable for accent/vowel-point-insensitive
// comparison: Greek -> lowercase, unaccented, final-sigma-normalized;
// Hebrew -> bare consonant skeleton (niqqud/cantillation stripped).
function normalizeWord(word, language) {
  if (!word) return '';
  if (language === 'hebrew') return stripHebrewPoints(word);
  if (language === 'greek') return stripGreekAccents(word);
  return word;
}

function buildIndex(db, table, language) {
  // SELECT * so the cached index can also serve `raw: true` lookups without
  // a second round-trip to the database.
  const rows = db.prepare(`SELECT * FROM ${table}`).all();

  const byWord = new Map();
  const byLemma = new Map();

  const addTo = (map, key, row) => {
    if (!key) return;
    let bucket = map.get(key);
    if (!bucket) {
      bucket = [];
      map.set(key, bucket);
    }
    bucket.push(row);
  };

  for (const row of rows) {
    addTo(byWord, normalizeWord(row.uword, language), row);
    addTo(byLemma, normalizeWord(row.uorig, language), row);
  }

  return { byWord, byLemma };
}

function getTableIndex(db, dbPath, table, language) {
  const cacheKey = `${dbPath}::${table}`;
  let index = tableIndexCache.get(cacheKey);
  if (!index) {
    index = buildIndex(db, table, language);
    tableIndexCache.set(cacheKey, index);
  }
  return index;
}

// -----------------------------------------------------------------------
// Greek morphology decoder
//
// fhlwhparsing.pro / .wform store parsing info as short letter codes
// (e.g. pro='v', wform='bapnsm') rather than English text. There is no
// legend table in the database; this mapping was reverse-engineered by
// cross-referencing hundreds of sample rows against known Koine Greek
// grammar (see the conversation this file was built in). Hebrew's
// lparsing.wform is already a human-readable Chinese description and
// lparsing.pro is always empty, so this decoder only applies to Greek.
//
// Confidence is high for: part of speech, mood, case, number, gender,
// person, and the common tenses/voices (present, imperfect, future,
// aorist, perfect, active, middle, passive). Lower confidence, rare codes
// are marked accordingly and the decoder falls back to the raw letter
// rather than guessing when a code isn't recognized at all.
// -----------------------------------------------------------------------

const GREEK_POS = {
  n: 'Noun',
  v: 'Verb',
  ra: 'Article',
  a: 'Adjective',
  d: 'Adverb',
  c: 'Conjunction',
  p: 'Preposition',
  t: 'Particle',
  i: 'Interjection',
  rp: 'Personal pronoun',
  rd: 'Demonstrative pronoun',
  rr: 'Relative pronoun',
  ri: 'Interrogative pronoun',
  ru: 'Indefinite pronoun',
  rf: 'Reflexive pronoun',
  rre: 'Reciprocal pronoun',
  dr: 'Relative adverb',
  crp: 'Conjunction-pronoun crasis', // e.g. κἀγώ = καί + ἐγώ
  ai: 'Interrogative adjective',
};

// Verb tense (wform position 1). Greek distinguishes a "weak"/1st and
// "strong"/2nd formation for aorist and perfect; both map to the same
// broad English tense name here.
const GREEK_TENSE = {
  p: { name: 'present', abbr: 'pres.' },
  i: { name: 'imperfect', abbr: 'impf.' },
  f: { name: 'future', abbr: 'fut.' },
  g: { name: 'future', abbr: 'fut.' }, // 2nd/irregular future - lower confidence
  a: { name: 'aorist', abbr: 'aor.' },
  b: { name: '2nd aorist', abbr: '2aor.' },
  x: { name: 'perfect', abbr: 'perf.' },
  c: { name: 'perfect', abbr: 'perf.' }, // strong/irregular perfect (e.g. οἶδα) - lower confidence
  d: { name: 'perfect', abbr: 'perf.' }, // perfect middle/passive forms
  y: { name: 'pluperfect', abbr: 'plup.' },
  e: { name: null, abbr: null }, // seen only on a transliterated Aramaic word (σαβαχθανι) - not a real Greek tense
};

// Verb voice (wform position 2).
const GREEK_VOICE = {
  a: { name: 'active', abbr: 'act.' },
  m: { name: 'middle', abbr: 'mid.' },
  p: { name: 'passive', abbr: 'pass.' },
  n: { name: 'middle (deponent)', abbr: 'mid. dep.' },
  o: { name: 'passive (deponent)', abbr: 'pass. dep.' },
};

// Verb mood (wform position 3).
const GREEK_MOOD = {
  i: { name: 'indicative', abbr: 'ind.' },
  s: { name: 'subjunctive', abbr: 'subj.' },
  o: { name: 'optative', abbr: 'opt.' },
  d: { name: 'imperative', abbr: 'impv.' },
  n: { name: 'infinitive', abbr: 'inf.' },
  p: { name: 'participle', abbr: 'ptc.' },
};

const GREEK_CASE = {
  n: { name: 'nominative', abbr: 'nom.' },
  g: { name: 'genitive', abbr: 'gen.' },
  d: { name: 'dative', abbr: 'dat.' },
  a: { name: 'accusative', abbr: 'acc.' },
  v: { name: 'vocative', abbr: 'voc.' },
};

const GREEK_NUMBER = {
  s: { name: 'singular', abbr: 'sg.' },
  p: { name: 'plural', abbr: 'pl.' },
};

const GREEK_GENDER = {
  m: { name: 'masculine', abbr: 'masc.' },
  f: { name: 'feminine', abbr: 'fem.' },
  n: { name: 'neuter', abbr: 'neut.' },
};

const GREEK_DEGREE = {
  c: { name: 'comparative', abbr: 'comp.' },
  s: { name: 'superlative', abbr: 'superl.' },
};

function decodeVerbForm(wform) {
  const tense = GREEK_TENSE[wform[0]];
  const voice = GREEK_VOICE[wform[1]];
  const mood = GREEK_MOOD[wform[2]];
  if (!tense || !voice || !mood) return null;

  const parts1 = [tense.abbr, voice.abbr, mood.abbr].filter(Boolean);

  if (mood.name === 'infinitive' && wform.length === 3) {
    return { line1: parts1.join(' '), line2: null, tense, voice, mood };
  }

  if (mood.name === 'participle' && wform.length === 6) {
    const kase = GREEK_CASE[wform[3]];
    const number = GREEK_NUMBER[wform[4]];
    const gender = GREEK_GENDER[wform[5]];
    if (!kase || !number || !gender) return null;
    return {
      line1: parts1.join(' '),
      line2: [kase.abbr, gender.abbr, number.abbr].join(' '),
      tense,
      voice,
      mood,
      case: kase,
      number,
      gender,
    };
  }

  if (wform.length === 5) {
    const person = /[123]/.test(wform[3]) ? wform[3] : null;
    const number = GREEK_NUMBER[wform[4]];
    if (!person || !number) return null;
    return {
      line1: parts1.join(' '),
      line2: `${ORDINAL_PERSON[person]} ${number.abbr}`,
      tense,
      voice,
      mood,
      person,
      number,
    };
  }

  return null;
}

const ORDINAL_PERSON = { 1: '1st', 2: '2nd', 3: '3rd' };

// Case+number+gender(+degree), used by nouns, adjectives, the article, and
// most declinable pronoun types (rd, rr, ri, ru, rre, ai).
function decodeDeclinableForm(wform) {
  if (wform.length < 3) return null;
  const kase = GREEK_CASE[wform[0]];
  const number = GREEK_NUMBER[wform[1]];
  const gender = GREEK_GENDER[wform[2]];
  if (!kase || !number || !gender) return null;
  const degree = wform.length > 3 ? GREEK_DEGREE[wform[3]] : null;
  const line1 = [kase.abbr, gender.abbr, number.abbr, degree ? degree.abbr : null]
    .filter(Boolean)
    .join(' ');
  return { line1, line2: null, case: kase, number, gender, degree: degree || null };
}

// Case+number+gender(optional, blank for 1st/2nd person)+person, used by
// personal (rp) and reflexive (rf) pronouns.
function decodePersonalForm(wform) {
  if (wform.length !== 4) return null;
  const kase = GREEK_CASE[wform[0]];
  const number = GREEK_NUMBER[wform[1]];
  const genderChar = wform[2];
  const gender = genderChar === ' ' ? null : GREEK_GENDER[genderChar];
  const person = /[123]/.test(wform[3]) ? wform[3] : null;
  if (!kase || !number || !person) return null;
  const line1 = [kase.abbr, gender ? gender.abbr : null, number.abbr]
    .filter(Boolean)
    .join(' ');
  return {
    line1,
    line2: `${ORDINAL_PERSON[person]} ${number.abbr}`,
    case: kase,
    number,
    gender: gender || null,
    person,
  };
}

/**
 * Decode a Greek `pro`/`wform` code pair from fhlwhparsing into a
 * human-readable description, e.g. pro='v', wform='bapnsm' (Ἰδὼν) ->
 * { partOfSpeech: 'Verb', partOfSpeechAbbr: 'V', line1: 'aor. act. ptc.',
 *   line2: 'nom. masc. sg.', ... }.
 *
 * Returns null if `pro` isn't a recognized code, or if `wform` doesn't
 * match a known shape for that part of speech (rather than guessing).
 *
 * @param {string} pro - the trimmed `pro` column value.
 * @param {string} wform - the `wform` column value (not trimmed - a
 *   leading/trailing space can be meaningful, e.g. for 1st/2nd person
 *   pronouns which have no gender).
 */
function decodeGreekMorphology(pro, wform) {
  const partOfSpeech = GREEK_POS[pro];
  if (!partOfSpeech) return null;

  const w = wform || '';
  let form = null;

  if (pro === 'v') {
    form = decodeVerbForm(w);
  } else if (pro === 'rp' || pro === 'rf') {
    form = decodePersonalForm(w);
  } else if (['n', 'a', 'ra', 'rd', 'rr', 'ri', 'ru', 'rre', 'ai'].includes(pro)) {
    form = decodeDeclinableForm(w);
  }
  // c, p, t, i, d, dr, crp are indeclinable in this dataset (no wform).

  return {
    partOfSpeech,
    partOfSpeechAbbr: pro.toUpperCase(),
    line1: form ? form.line1 : null,
    line2: form ? form.line2 : null,
    ...form,
  };
}

function toResult(row, language) {
  const pro = row.pro ? row.pro.trim() : null;
  const wform = row.wform ? row.wform.trim() : null;

  return {
    book: row.engs,
    chapter: row.chap,
    verse: row.sec,
    wordPosition: row.wid,
    strongNumber: row.sn ? row.sn.trim() : null,
    partOfSpeech: pro,
    morphology: wform,
    // Decoded English description of pro/wform (Greek only - see
    // decodeGreekMorphology; Hebrew's wform is already a Chinese
    // description, so this is null for lparsing rows).
    parsedForm: language === 'greek' && pro ? decodeGreekMorphology(pro, row.wform || '') : null,
    gloss: row.exp,
    inflectedWord: row.uword,
    lemma: row.uorig,
    transliteratedWord: row.word,
    transliteratedLemma: row.orig,
  };
}

/**
 * Look up morphological parsing information for a Greek or Hebrew word
 * against bible_parsing.db (tables `fhlwhparsing` for Greek NT words,
 * `lparsing` for Hebrew OT words).
 *
 * The word can be given inflected or as a lemma, with or without
 * accents/niqqud - matching ignores Greek accents/breathings and Hebrew
 * vowel points/cantillation by default.
 *
 * @param {string} word - a Greek or Hebrew word.
 * @param {object} [options]
 * @param {string} [options.dbPath] - path to bible_parsing.db (defaults to
 *   the copy next to this file).
 * @param {boolean} [options.exact=false] - match the stored text exactly,
 *   including accents/niqqud, instead of the normalized comparison.
 * @param {boolean} [options.lemmaOnly=false] - only match against the
 *   dictionary/lemma form (uorig) rather than also matching inflected
 *   occurrences (uword).
 * @param {boolean} [options.raw=false] - return the complete database row
 *   (every column, including id/remark/username/modtime/osn) instead of
 *   the curated subset of fields.
 * @returns {Array<object>} one entry per matching occurrence in the text,
 *   each with book/chapter/verse/word-position and morphology info.
 */
function getParsingInfo(word, options = {}) {
  if (typeof word !== 'string' || !word.trim()) {
    throw new Error('word must be a non-empty string');
  }

  const { dbPath = DEFAULT_DB_PATH, exact = false, lemmaOnly = false, raw = false } = options;

  const language = detectLanguage(word);
  if (!language) {
    throw new Error(`"${word}" does not look like Greek or Hebrew text.`);
  }

  const table = language === 'greek' ? 'fhlwhparsing' : 'lparsing';
  const db = new Database(dbPath, { readonly: true, fileMustExist: true });

  try {
    let rows;

    if (exact) {
      const sql = lemmaOnly
        ? `SELECT * FROM ${table} WHERE uorig = ? ORDER BY id`
        : `SELECT * FROM ${table} WHERE uword = ? OR uorig = ? ORDER BY id`;
      const stmt = db.prepare(sql);
      rows = lemmaOnly ? stmt.all(word) : stmt.all(word, word);
    } else {
      const { byWord, byLemma } = getTableIndex(db, dbPath, table, language);
      const key = normalizeWord(word, language);
      const fromWord = lemmaOnly ? [] : byWord.get(key) || [];
      const fromLemma = byLemma.get(key) || [];
      // De-duplicate in case a word's normalized inflected form and
      // normalized lemma form happen to coincide for the same row.
      const seen = new Set();
      rows = [];
      for (const row of [...fromWord, ...fromLemma]) {
        if (!seen.has(row.id)) {
          seen.add(row.id);
          rows.push(row);
        }
      }
      rows.sort((a, b) => a.id - b.id);
    }

    return raw ? rows : rows.map((row) => toResult(row, language));
  } finally {
    db.close();
  }
}

module.exports = { getParsingInfo, detectLanguage, normalizeWord, decodeGreekMorphology };
