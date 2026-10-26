# Project instructions

Read [SPEC.md](SPEC.md) before changing product behavior. All current work targets the standalone web application, including backend changes needed by its scenarios. Start Chrome extension (plugin) work only after the user explicitly approves working on it; completing web work or encountering an old extension issue does not provide that approval. See SPEC §2.1 for the scope and deferred acceptance criteria.

The MVP runs locally through Docker Compose with separate username/password accounts; each learner has a private learning profile and their own Codex/ChatGPT subscription. Keep requirements and acceptance criteria consistent when scope changes.

Implement agent pipelines with Pydantic AI. For agent, model-provider, structured-output, dependency, history, or MCP work, read the local [building-pydantic-ai-agents skill](.agents/skills/building-pydantic-ai-agents/SKILL.md) and only the references relevant to the task. Check examples against the project's pinned library version.

Write clear, concise paragraphs in plain language. Explain the outcome and the evidence needed to assess it. Avoid unnecessary jargon and canned conclusions.

## Agent skills

### Issue tracker

Track work in GitHub Issues for `GlebNM/language_tutor`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.
