# Project instructions

Read [SPEC.md](SPEC.md) before changing product behavior. The MVP runs locally through Docker Compose for one learner. The standalone web application and Chrome extension open the same learning profile directly; AI uses the learner's Codex/ChatGPT subscription. Keep requirements and acceptance criteria consistent when scope changes.

Implement agent pipelines with Pydantic AI. For agent, model-provider, structured-output, dependency, history, or MCP work, read the local [building-pydantic-ai-agents skill](.agents/skills/building-pydantic-ai-agents/SKILL.md) and only the references relevant to the task. Check examples against the project's pinned library version.

Write clear, concise paragraphs in plain language. Explain the outcome and the evidence needed to assess it. Avoid unnecessary jargon and canned conclusions.

## Agent skills

### Issue tracker

Track work in GitHub Issues for `GlebNM/language_tutor`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.
