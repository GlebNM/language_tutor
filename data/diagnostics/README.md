# Diagnostic data v1

The 12 English words and two original sentences in `items-en-v1.json` have stable IDs and permitted Russian meanings. The implementation checks that four words belong to each declared frequency group. The meanings include ordinary synonyms; the agent may accept equivalent free text. Sentences refine the description only. The small sample is a heuristic, not a CEFR certification. Content review and source metadata are stored with the pool; later editorial changes require a new pool version.

`frequency-en-v1.tsv` is derived from Robyn Speer's **wordfreq 3.1.1** English large list (`top_n_list('en', 20000)`), generated on 19 October 2026. In source order, casefold, normalize curly apostrophes to ASCII, retain strings matching `[a-z]+(?:'[a-z]+)?`, remove duplicates, then take 6000 entries and assign ranks 1 through 6000. Ranks describe this normalized list; they are not CEFR labels or probabilities. Lemmas absent from the list receive no prior. No runtime download or wordfreq dependency is required.

The derived frequency data is distributed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), with the complete terms in `CC-BY-SA-4.0.txt`. Attribution: Robyn Speer, wordfreq; SUBTLEX authors Marc Brysbaert et al.; OpenSubtitles and other upstream sources listed in `WORD FREQ NOTICE.md`. SUBTLEX is freely available data. The wordfreq code is Apache 2.0 and is not bundled here.

Source: [wordfreq repository](https://github.com/rspeer/wordfreq), [upstream attribution notice](https://github.com/rspeer/wordfreq/blob/master/NOTICE.md). Changes are the filtering, normalization and rank assignment described above.
