/**
 * util.js — pure helpers: HTML escape, citation-skip set, question validation.
 */
export function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Fields to skip when rendering a citation quote object generically.
export const QUOTE_SKIP = new Set([
  'bullet_id', 'bullet_idx', 'id', 'knesset', 'knesset_num',
  'mk_individual_id', 'committee_id', 'faction_id', 'position_id',
]);

// Mirrors server-side _QUESTION_RE: Hebrew block + whitespace + digits + basic punctuation.
const QUESTION_RE = /^[֐-׿\s\d.,?!:\-"']+$/;
const MAX_QUESTION = 2000;

export function validateQuestion(q) {
  if (!q) return null;
  if (q.length > MAX_QUESTION) return `השאלה ארוכה מדי (מקסימום ${MAX_QUESTION} תווים)`;
  if (!QUESTION_RE.test(q)) return 'יש להזין שאלה בעברית בלבד';
  return null;
}
