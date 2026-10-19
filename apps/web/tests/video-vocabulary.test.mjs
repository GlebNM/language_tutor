import test from "node:test";
import assert from "node:assert/strict";
import { filterVideoVocabulary, groupVideoVocabulary } from "../src/videoVocabulary.ts";

const knowledge = (score, options = {}) => ({
  score,
  ignored: options.ignored ?? false,
  source: options.source ?? (score == null ? null : "manual"),
  provisional: options.provisional ?? false,
  status: score == null ? "unassessed" : "known",
});
function token(id, lemma, pos, surface, score, lexeme = `${lemma}-${pos}`, extra = {}) {
  return {id, material_id:"m1", cue_id:`c${id}`, lexeme_id:lexeme, surface, lemma, part_of_speech:pos, language:"en", start_offset:0, end_offset:surface.length, uncertain:false, knowledge:knowledge(score, extra)};
}
function cue(id, tokens) { return {id:`c${id}`, start_ms:id * 1000, end_ms:id * 1000 + 500, text:tokens.map(item => item.surface).join(" "), original_text:"", tokens}; }

test("groups all full-track forms by lexeme and part of speech, then sorts score and count", () => {
  const cues = [
    cue(1, [token("1", "run", "VERB", "runs", 2), token("2", "run", "NOUN", "run", 0), token("3", "bright", "ADJ", "bright", null)]),
    cue(2, [token("4", "run", "VERB", "running", 2), token("5", "run", "NOUN", "runs", 0), token("6", "quiet", "ADJ", "quiet", 0), token("7", "slow", "ADJ", "slow", 1), token("8", "fast", "ADV", "fast", 3)]),
  ];
  const entries = groupVideoVocabulary(cues);
  assert.deepEqual(entries.map(entry => [entry.lemma, entry.part_of_speech, entry.count]), [
    ["bright", "ADJ", 1], ["run", "NOUN", 2], ["quiet", "ADJ", 1], ["slow", "ADJ", 1], ["run", "VERB", 2], ["fast", "ADV", 1],
  ]);
  const verb = entries.find(entry => entry.part_of_speech === "VERB");
  assert.deepEqual(verb.forms, ["runs", "running"]);
  assert.deepEqual(verb.occurrences.map(item => item.cue.start_ms), [1000, 2000]);
});

test("filters each score and ignored words separately and searches forms", () => {
  const entries = groupVideoVocabulary([cue(1, [
    token("1", "hello", "INTJ", "hello", null),
    token("2", "world", "NOUN", "world", 1, undefined, {ignored:true, provisional:true}),
  ])]);
  assert.deepEqual(filterVideoVocabulary(entries, "HEL", "unassessed", "false").map(item => item.lemma), ["hello"]);
  assert.deepEqual(filterVideoVocabulary(entries, "", "1", "true").map(item => item.lemma), ["world"]);
  assert.equal(filterVideoVocabulary(entries, "", "0", "").length, 0);
  assert.equal(filterVideoVocabulary(entries, "ORL", "", "")[0].knowledge.provisional, true);
});
