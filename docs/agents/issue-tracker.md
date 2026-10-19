# Issue tracker: GitHub

Track work in GitHub Issues for `GlebNM/language_tutor`. Use the `gh` CLI from this checkout; it infers the repository from `origin`. When running elsewhere, pass `--repo GlebNM/language_tutor`.

## Conventions

- Create an issue: `gh issue create --title "..." --body-file /path/to/body.md`.
- Read an issue: `gh issue view <number> --json number,title,body,labels,comments`.
- List issues: `gh issue list --state open --json number,title,body,labels,comments`. Add label or state filters as needed; use `--limit` or pagination when completeness matters.
- Edit an issue body: `gh issue edit <number> --body-file /path/to/body.md`.
- Comment: `gh issue comment <number> --body-file /path/to/comment.md`.
- Apply or remove labels: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number>`.

Write multiline bodies and comments to a temporary file and pass it with `--body-file`. Publish or comment only when the user or an explicitly invoked skill authorizes it.

When a skill says "publish to the issue tracker", create a GitHub issue. When it says "fetch the relevant ticket", read the issue with its labels and comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares issue and pull request numbers. When a reference is ambiguous, try `gh pr view <number>` and fall back to `gh issue view <number>`. If this flag is changed to `yes`, use `gh pr view`, `gh pr diff`, `gh pr comment`, `gh pr edit`, and `gh pr close` for the corresponding triage operations.

## Wayfinding operations

Used by `/wayfinder`. A map is one issue containing Notes, Decisions-so-far, and Fog, with child issues as tickets.

- Map: create an issue with label `wayfinder:map`.
- Child: use `gh issue create --parent <map-number> ...` or `gh issue edit <map-number> --add-sub-issue <child-number>`. If sub-issues are unavailable, add the child to a task list in the map and start its body with `Part of #<map-number>`.
- Type: use `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`. Create a required label if absent when the workflow is invoked.
- Blocking: use native issue dependencies with `gh issue edit <child-number> --add-blocked-by <blocker-number>`. If unavailable, start the child body with `Blocked by: #<number>, #<number>`. A ticket is unblocked when every blocker is closed.
- Frontier: inspect the map's open children in map order, exclude tickets with open blockers or an assignee, and select the first remaining ticket.
- Claim: assign the ticket with `gh issue edit <number> --add-assignee @me` as the workflow's first write.
- Resolve: comment with the result, close the ticket, and append a concise result and link to the map's Decisions-so-far section.
