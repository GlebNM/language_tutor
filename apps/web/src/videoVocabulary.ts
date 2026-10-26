import type { Cue, Knowledge, Token } from "./api";

export type VideoVocabularyEntry = {
  lexeme_id: string;
  lemma: string;
  part_of_speech: string;
  forms: string[];
  count: number;
  knowledge: Knowledge;
  occurrences: Array<{ cue: Cue; token: Token }>;
};

export function groupVideoVocabulary(cues: Cue[]): VideoVocabularyEntry[] {
  const grouped = new Map<string, VideoVocabularyEntry>();
  for (const cue of cues) {
    for (const token of cue.tokens) {
      if (token.vocabulary?.eligible === false) continue;
      const key = token.lexeme_id || `${token.lemma.toLocaleLowerCase()}\u0000${token.part_of_speech}`;
      let entry = grouped.get(key);
      if (!entry) {
        entry = {
          lexeme_id: token.lexeme_id,
          lemma: token.lemma,
          part_of_speech: token.part_of_speech,
          forms: [],
          count: 0,
          knowledge: token.knowledge ?? { score: null, ignored: false, source: null, provisional: false, status: "unassessed" },
          occurrences: [],
        };
        grouped.set(key, entry);
      }
      if (!entry.forms.includes(token.surface)) entry.forms.push(token.surface);
      entry.count += 1;
      entry.occurrences.push({ cue, token });
      // Knowledge is shared by lexeme. Prefer an occurrence that carries the current value.
      if (token.knowledge) entry.knowledge = token.knowledge;
    }
  }
  return [...grouped.values()].sort((left, right) => {
    const level = (knowledge: Knowledge) => knowledge.score == null ? 0 : knowledge.score + 1;
    return level(left.knowledge) - level(right.knowledge)
      || right.count - left.count
      || left.lemma.localeCompare(right.lemma)
      || left.part_of_speech.localeCompare(right.part_of_speech);
  });
}

export function filterVideoVocabulary(
  entries: VideoVocabularyEntry[],
  search: string,
  score: string,
  ignored: string,
): VideoVocabularyEntry[] {
  const query = search.trim().toLocaleLowerCase();
  return entries.filter(entry => {
    if (query && ![entry.lemma, entry.part_of_speech, ...entry.forms].some(value => value.toLocaleLowerCase().includes(query))) return false;
    if (score === "unassessed" && entry.knowledge.score != null) return false;
    if (["0", "1", "2", "3"].includes(score) && String(entry.knowledge.score) !== score) return false;
    if (ignored === "false" && entry.knowledge.ignored) return false;
    if (ignored === "true" && !entry.knowledge.ignored) return false;
    return true;
  });
}
