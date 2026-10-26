## Problem Statement

Language Tutor currently treats learning materials as YouTube videos with timed subtitle cues. Learners cannot add and read ordinary texts with the same personal dictionary, expression meanings, contextual cards and learning agent.

The learner also needs new reading material that naturally contains vocabulary appropriate to their personal dictionary. The purpose is recognizing words and phrases in context. A list of words to produce, comprehension quizzes and searches for videos containing particular vocabulary do not address the agreed scenario.

## Solution

Deliver two chapters, in order. Chapter 1 adds text as a first-class material alongside video, with manual text entry, TXT import and a reading interface. Chapter 2 adds generation: a learner opens a window with exactly four settings—optional topic, optional style, length and lexical difficulty—and receives a successfully checked text in their library.

A writing agent can read the learner's dictionary and find their existing texts containing a word. Application code measures lexical load. A requirements reviewer checks the requested topic and style and the text's coherence. A separate context reviewer receives the complete draft and an automatically collected list of insufficiently familiar words and their occurrences. It checks whether the surrounding text helps reveal their meaning. The writer receives all blocking feedback and revises within a bounded job. Only a version that passes every required check is added to the library.

## User Stories

1. As a learner, I want to paste an English text into my library, so that I can study it with my personal dictionary.
2. As a learner, I want to import a UTF-8 TXT file, so that I can read material I already have.
3. As a learner, I want to give a text a title, so that I can recognize it in my library.
4. As a learner, I want to see videos and texts in the same library, so that I can choose my next material in one place.
5. As a learner, I want to filter the library by material type, so that I can find reading material quickly.
6. As a learner, I want to search my library, so that I can reopen a particular text.
7. As a learner, I want a reading page with paragraphs and no video controls, so that I can focus on the text.
8. As a learner, I want word highlighting to reflect my personal familiarity, so that I can see where help may be useful.
9. As a learner, I want to open a contextual card from a word in a paragraph, so that I can understand its meaning there.
10. As a learner, I want word forms to share the same dictionary entry when appropriate, so that reading updates the same vocabulary as video study.
11. As a learner, I want expressions to retain their own meanings and familiarity, so that familiar component words do not hide an unfamiliar phrase.
12. As a learner, I want separated parts of a phrasal verb to remain connected, so that I can study the whole expression.
13. As a learner, I want manual familiarity changes and explicit resets to work in texts, so that I control my dictionary.
14. As a learner, I want familiarity changes made while reading to apply in videos and conversations, so that my vocabulary stays consistent.
15. As a learner, I want to search within a text, so that I can return to a passage.
16. As a learner, I want my reading position restored, so that I can continue later.
17. As a learner, I want dictionary examples and agent citations to reopen the correct paragraph, so that I can recover their original context.
18. As a learner, I want to discuss a text with the learning agent, so that I can ask about words and expressions in the material.
19. As a learner, I want stored texts and dictionary data to remain available without AI, so that reading can continue when my subscription is unavailable.
20. As a learner, I want existing videos, cards and history to survive the change, so that adding texts does not lose my previous work.
21. As a learner, I want text versions to retain stable original passages, so that existing cards and examples continue to refer to the right content.
22. As a learner, I want a generation window with only topic, style, length and difficulty, so that creating a text stays simple.
23. As a learner, I want to leave the topic empty, so that I can generate a suitable text without inventing a prompt.
24. As a learner, I want to leave the style empty, so that I can accept a natural default presentation.
25. As a learner, I want to choose a short, medium or long text, so that reading fits my available time.
26. As a learner, I want difficulty based on my current dictionary, so that the selected load reflects vocabulary I actually recognize or am learning.
27. As a learner, I want separate limits for learning and unfamiliar vocabulary, so that I can control both kinds of load.
28. As a learner, I want unassessed words distinguished from words I explicitly do not know, so that uncertainty is visible.
29. As a learner, I want provisional diagnostic familiarity identified, so that the application does not present assumptions as confirmed knowledge.
30. As a learner, I want the writer to choose vocabulary naturally and adjust it during revisions, so that I receive a coherent text without managing a mandatory word list.
31. As a learner, I want the writer to know both familiar and learning vocabulary, so that surrounding language can make new vocabulary accessible.
32. As a learner, I want the writer to find my texts containing a word across its forms, so that it can use actual previous contexts to adapt its writing.
33. As a learner, I want the writer to read a retrieved text in full when needed, so that it can understand more than an isolated search snippet.
34. As a learner, I want generated texts to fit my requested topic and style, so that the material interests me.
35. As a learner, I want a separate review of how context reveals unfamiliar words, so that the text supports recognition while reading.
36. As a learner, I want the context reviewer to consider the complete text, so that helpful context in another sentence or paragraph is recognized.
37. As a learner, I want weak contexts and exceeded limits returned to the writer, so that the text can improve before it is added.
38. As a learner, I want every revised version checked again, so that one improvement does not leave another requirement broken.
39. As a learner, I want generation progress and cancellation, so that I can use the application while a text is being prepared.
40. As a learner, I want a successfully checked text added automatically, so that I can start reading it from my library.
41. As a learner, I want generated texts identified as AI-created, so that I understand their origin.
42. As a learner, I want failed generation to show a draft and concrete reasons, so that I can decide whether to retry or change my settings.
43. As a learner, I want retries and repeated completion requests to avoid duplicate materials, so that my library remains usable.
44. As a learner, I want dictionary changes during generation handled consistently, so that the saved difficulty is based on an identifiable current profile.
45. As a learner, I want generation to use my saved AI settings and personal subscription, so that I can use the same connection as other AI features.
46. As a learner, I want each account's texts and retrieved examples kept private, so that another learner cannot access them through tools or generation.
47. As a learner, I want deletion and cancellation to prevent late results from reappearing, so that my actions remain effective.
48. As a learner, I want creating, opening and finishing a text to leave familiarity under my control, so that activity is not mistaken for vocabulary knowledge.

## Implementation Decisions

### Chapter 1: text materials

- Extend the existing material module to support video and text. Shared metadata includes identity, title, language, origin, content hash, version and creation time. YouTube identity belongs only to video metadata.
- Represent content as ordered material blocks. A subtitle block has valid start and end times; a text paragraph has text and no time. Do not create fictitious YouTube identifiers or timestamps for text materials.
- Bind word occurrences to a block, with their surface form, offsets and language-specific lexical identity. Reuse the existing language adapter, automatic vocabulary selection, personal familiarity and expression meanings.
- Migrate existing video cues into the common representation while preserving identifiers and relationships used by contextual cards, expressions, conversations and learning activity. Existing video playback and caption operations must continue to work.
- Preserve immutable original content for saved versions. Editing an existing text creates a version whose content does not overwrite the sources of earlier cards or examples.
- Add manual text entry and UTF-8 TXT import with a title. Use existing material size and validation policies where applicable. Treat text as data, including literal markup-like content.
- Add the reading interface, text search and reading-position restoration. Reuse the common word and expression interactions, dictionary controls and contextual cards.
- Generalize material context and citations through the existing learning-context interface and MCP. A text citation opens its material and paragraph; a video citation retains its timestamp behavior.
- Respect accepted decisions: original conversation messages remain separate from model history; automatic vocabulary selection remains separate from manual familiarity; expression familiarity belongs to its meaning and is not calculated from component words. Text support must not expose the contents of other conversations.
- Keep reading activity distinct from listened subtitle cues. Reading activity does not automatically change familiarity or count as video playback time.

### Chapter 2: settings and lexical policy

- The generation window has exactly four settings: optional topic, optional style, length and difficulty. Do not add advanced generation controls for source material, source relationship, format, vocabulary selection, target counts, repetition counts or exact percentage tuning.
- Length presets are short at 150–200 words, medium at 250–350 words and long at 450–550 words. Default to medium length and gentle difficulty. Topic and style may both be absent. The language, model and reasoning come from the learner's existing settings.
- The writer chooses vocabulary freely from the learner's profile and may change it between revisions. There is no selected-target entity, mandatory word list, minimum number of lexical targets or repetition quota.
- Use a distinct, versioned generation policy. K contains word occurrences with familiarity 2 or 3; L contains familiarity 1; U0 contains explicit familiarity 0; Ua contains missing or null familiarity; U is U0 plus Ua. Missing familiarity must not be presented as proven ignorance.
- Calculate over eligible occurrences in the title and paragraphs using the current automatic selection policy, manual priority, ignored decisions and diagnostic rules. Ordinary function words count. Use the same resolver for existing and not-yet-registered lexical identities, without registering words merely to assess a draft.
- Gentle difficulty allows L up to 4% and U up to 1%; normal allows L up to 7% and U up to 3%; rich allows L up to 10% and U up to 5%. Both maxima must pass. No minimum L is imposed. N is K plus L plus U; N equal to zero is not a successful assessment.
- Compare exact counts with the permitted ratios before rounding for display. For 300 eligible occurrences, normal permits at most 21 L and 9 U occurrences. Count unique lexemes as information only; do not add hidden unique-word caps.
- Preserve the current video-difficulty policy separately, including its existing treatment of familiarity 0 and 1. Do not silently redefine historical video percentages.
- Expression meanings retain separate familiarity and semantic context. Do not add expression and component-word percentages together, or introduce new expression quotas. A count based on familiar component words is not proof that a phrase's meaning is familiar.
- Present provisional familiarity separately. Do not infer CEFR or assume common words are known solely because of frequency. If the profile cannot support the requested assessment, return insufficient-profile guidance using the existing diagnostic workflow.

### Agent roles and contracts

- Use the project's pinned Pydantic AI version and existing subscription connection. The main roles are text_writer, requirements_reviewer and context_reviewer. Application code controls required transitions, budgets and the final save.
- GenerationRequest contains topic and style as optional strings, length as short/medium/long, and difficulty as gentle/normal/rich. The server normalizes this into immutable GenerationConstraints containing the language, actual length range, L/U maxima, applicable topic/style criteria and rule versions.
- A job context contains the authenticated learner, storage generation, profile revision, dictionary snapshot, normalized constraints, saved AI settings, usage and remaining budget. Tool access is authorized by the server session, never by a learner identifier supplied by the model.
- WritingProfile contains counts by familiarity, active diagnostic rules, provenance of provisional familiarity, and small examples of known and learning vocabulary. It describes the learner's dictionary rather than a manufactured language level. Additional vocabulary is available through bounded tools.
- WriterInput contains GenerationConstraints, WritingProfile and, for revisions, the prior VersionedDraft and RevisionFeedback. The writer can retrieve dictionary knowledge, expression meanings and existing text contexts. It does not require a user-chosen source material.
- WriterOutput is a title and ordered paragraph texts. An alternative blocked result contains a reason and conflicting constraints. The writer cannot set its own measured percentages, declare the job passed, change dictionary familiarity or save a completed material.
- Application code assigns draft identity, revision, content hash, paragraph and token identifiers, tokenizes the draft, determines lemmas and parts of speech, and resolves familiarity against the snapshot. Reports must refer to that same draft and snapshot.
- ComplexityReport contains length, eligible occurrence count, K/L/U0/Ua counts and ratios, informational unique counts, actual violations and every insufficiently familiar item with familiarity, provenance and concrete occurrences. Items with familiarity 1 are not failures merely because they are learning words. A complete report can use pagination; measurements always cover the whole draft.
- Build ContextReviewInput from the current draft analysis: group all eligible word occurrences with familiarity 0, 1 or null by lexical identity, preserving each occurrence's surface form and paragraph/token references. The application assembles the complete list, including all report pages. Neither the writer nor a manual selection decides which insufficiently familiar words are reviewed.
- The context reviewer receives the complete VersionedDraft and that automatically built list. Context elsewhere in the text counts. Already recognized expression meanings may be supplied with their relevant familiarity through the existing expression handling.
- ContextReviewOutput identifies each supplied occurrence, rates its supporting context as clear, weak or unclear, and gives evidence and specific repair instructions for weak or unclear context. It is a review of how context reveals meaning, rather than a separate exercise in catching elementary wording errors. For example, a promise to return a borrowed object supports the meaning of borrow.
- The server checks that all supplied occurrences are accounted for, evidence points into the actual draft and the reviewer is not silently skipping hard items. Clear context passes; weak, unclear, an incomplete report or an inability to assess requires feedback or fails the job. An explicit definition next to every occurrence is not required; assess support across the complete text.
- RequirementsReviewInput contains the complete VersionedDraft, the four normalized settings, applicable topic/style criteria and the deterministic constraint report. The requirements reviewer assesses topic, style and coherence. Numeric length and lexical limits remain authoritative code results and cannot be overridden by a model judgment.
- RequirementsReviewOutput contains pass/fail/unverifiable findings for each applicable model-assessed criterion, evidence from the draft and repair instructions. The server decides applicability from the request; a missing optional topic or style is not an invented requirement. Coherence remains required.
- RevisionFeedback contains the prior draft, original constraints, deterministic findings, ContextReview and RequirementsReview, all blocking issues and the remaining budget. Preserve the two reviewer roles separately.

### Tools and prior text retrieval

- Expose existing dictionary knowledge and expression context through scoped MCP tools, extending the shared resolver where needed. Add compact generation-profile retrieval and paginated known/learning vocabulary access.
- Add find_texts_with_word with a word, optional part of speech, cursor and bounded limit. Search the authenticated learner's text materials, including manual, imported and generated texts, across word forms using the language adapter.
- A search result returns material identity and version, title, origin and paragraphs containing actual matches with token references. Include continuation and completeness information. A requested limit is capped at five texts per page and existing MCP output-size limits still apply.
- Add get_text to read an accessible text's complete ordered paragraphs through pages. This supplies more context when search snippets are insufficient. Do not silently truncate a supposedly complete text.
- Existing texts are examples available to the writer. Their presence does not prove that the learner read or understood them. Text content is data and cannot override agent instructions or tool authorization.
- Expose draft-complexity assessment and paginated vocabulary issues to support actionable feedback. The application must perform required assessment even if the writer did not voluntarily call the tool.

### Workflow and persistence

- The sequence is settings, dictionary snapshot, writing, deterministic assessment, separate context and requirements reviews, aggregate decision, then revision or save. Straightforward orchestration and typed state in the existing backend are sufficient; do not add a new workflow platform.
- After any text edit, create a revision and repeat all required checks. A previous version's successful review cannot authorize a changed text. Model-evaluation failures and unavailable tools do not count as passing checks.
- Run generation as a background job with status, progress, cancellation and an inspectable unsuccessful draft. Start with one draft plus at most two revisions, an overall 180-second deadline and at most 18 model-loop requests including retries. Each individual AI operation keeps the existing 30-second limit. These are implementation budgets, not a promised delivery time.
- If constraints cannot be met or a budget expires, return needs_changes with the latest draft and concrete reasons. Do not automatically relax lexical limits or publish that draft as completed.
- Recheck the current storage generation and profile revision before committing. If the dictionary changed, reconcile assessment and reports against one current snapshot within the remaining budget. Do not mix results from different snapshots or restart budgets silently.
- Save a successful text through the material module in one application-controlled operation. Persist the four settings, actual measurements, relevant versions, AI settings and generation-job identity. A job identity can produce at most one completed material, including after retries or repeated completion requests.
- Cancellation, data reset and account deletion must block late publication. Existing subscription-disconnection rules apply to pending AI operations. After restart, an unfinished job is interrupted and can be explicitly retried.
- During implementation, update the product specification, acceptance criteria and domain terminology for text materials and generation. Preserve the local Docker Compose delivery and separate learner accounts.

## Testing Decisions

- Proposed primary test seam: the existing authenticated application interface, exercised through public HTTP operations. Test complete visible outcomes of material creation, reading-related selections, generation jobs, assessment feedback and saved results. Avoid assertions about private helper calls, prompt wording or internal class organization. The user authorized proceeding with implementation and verification using this discussed approach.
- Use controlled Pydantic AI model responses through the existing model-injection mechanism to drive success, feedback and failure cases. Keep actual persistence, dictionary resolution and a real internal MCP server in integration checks. Validate tool retrieval through public agent/MCP behavior, not by bypassing authorization.
- Existing material import/reopen/version tests, conversational MCP and source-history tests, caption-improvement background-job tests, and multi-account ownership tests provide prior art. Extend those patterns instead of creating separate test-only application interfaces.
- Test chapter 1 through adding text, reopening after restart, selecting an inflected word, sharing familiarity with video, opening a correct source paragraph and preserving old video data through migration. Include text validation, expressions and ownership.
- Test exact L/U boundaries and rounding, explicit unknown versus unassessed states, prior versus manual overrides, ignored/excluded occurrences, empty profiles and distinct parts of speech. Verify all three preset pairs and the absence of hidden unique-word or repetition constraints.
- Test that the context review receives the complete draft and the automatically assembled full 0/1/null occurrence list. Include a word whose meaning is supported by a different sentence or paragraph, a weak context requiring revision, multiple occurrences, missing review entries and invalid evidence references.
- Test independent reviewers: a draft can pass theme/style and fail context, or pass context and fail theme/style. A numeric failure remains blocking even if model reviewers approve. Any revision invalidates the previous reports.
- Test the retrieval tools with inflected forms, part-of-speech ambiguity, multiple texts, full-text pagination, unavailable material and another learner's identifiers. Search results must reflect real stored passages.
- Test a writer changing its vocabulary between drafts without maintaining selected-target identifiers. Verify settings contain only topic, style, length and difficulty.
- Test budgets, provider failure, cancellation, reset, profile changes, restart and idempotent completion through public job operations. No failed or late result may appear as a completed material.
- Add a small browser verification of text creation and reading, shared word controls, the four-setting dialog, progress and opening a completed generated material. This verifies the visible interface while the application-interface tests cover the larger behavior matrix.
- Run a small separate live-subscription evaluation with human judgment of naturalness, contextual support, useful feedback and observed latency. Controlled models establish workflow correctness; they do not establish language quality or learning effectiveness.

## Out of Scope

- Chrome extension work, new video platforms, speech generation, audio transcription and voice practice.
- Video recommendations or searches for videos containing particular learning words.
- Comprehension quizzes, mini-tests and requiring the learner to produce vocabulary.
- Source-material selection, adaptation modes, a generation-format selector or a vocabulary-selection control.
- Selected lexical targets, mandatory inclusion lists, target counts, repetition quotas and hidden unique-word or expression caps.
- CEFR inference or using a global frequency list as proof of personal familiarity.
- Automatic familiarity changes from generating or reading a text, full SRS/Anki scheduling and a new reading-time analytics system.
- Website scraping, PDF import and rich document editing beyond the agreed text-entry/TXT reading scope.
- A new AI provider, a public hosted service, new workflow infrastructure or model changes outside the learner's existing AI settings.

## Further Notes

- User corrections supersede earlier brainstorming. Keep two implementation chapters and exactly four generation settings. U maxima are 1% / 3% / 5%; L maxima are 4% / 7% / 10%.
- Keep a dedicated context reviewer. Its core input is the full draft plus insufficiently familiar words derived by tokenization, lemmatization, part-of-speech analysis and dictionary lookup on that draft.
- The model-review process follows the evaluator–optimizer pattern described by [Anthropic](https://www.anthropic.com/research/building-effective-agents), with application-controlled hand-off and typed results as described by [Pydantic AI](https://pydantic.dev/docs/ai/guides/multi-agent-applications/) and its [output documentation](https://pydantic.dev/docs/ai/core-concepts/output/). The project uses its pinned version, verified as 2.57.0 during design.
- Numeric limits are product settings. They are not guarantees of comprehension, and a model's context judgment is not a measurement of the learner's understanding.
- The user explicitly requested implementation, parallel subagents and complete verification. Use the discussed authenticated application interface, real MCP and browser acceptance checks.
