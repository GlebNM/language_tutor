# Domain docs

Use a single-context layout: `GLOSSARY.md` at the repository root and architecture decision records (ADRs) in `docs/adr/`.

## Before exploring

Read `GLOSSARY.md` and the ADRs relevant to the area being explored. If `GLOSSARY-MAP.md` is introduced later, follow it to the relevant context glossaries and also read any relevant `src/<context>/docs/adr/` records.

If these files or directories do not exist, proceed silently. Do not propose creating them upfront. `/domain-modeling`, also used by `/grill-with-docs` and `/improve-codebase-architecture`, creates them as terms and decisions are resolved.

## Use the glossary's vocabulary

Use the defined terms in issue titles, proposals, hypotheses, code, and tests. Follow any guidance about synonyms to avoid. If a concept is missing, reconsider whether it fits the project or note the gap for `/domain-modeling`.

## Flag ADR conflicts

If a proposal conflicts with an existing ADR, identify the ADR and explain why the decision should be reconsidered.
