## Project Workflow

- This project should not keep long-lived extra working branches for ordinary Codex changes.
- `master` is the pre-multitenant stable line until the tenant architecture is promoted.
- Multitenant/AuthNode/FastReAct integration work must be committed to and pushed on the `tenant` branch.
- Do not merge tenant work back into `master` unless the user explicitly asks to promote `tenant` as the new mainline.
- When GitHub authentication is needed for push/merge operations, use the PAT stored at `~/Github_PAT.txt`; never write the token into the repository, logs, commit messages, or user-visible command output.

## Implementation Constraints

- PSKA is a domain-agnostic knowledge system. Do not improve answer quality with domain-specific hardcoded rules, fixture-specific shortcuts, or question-specific special cases.
- If a behavior needs higher answer quality, implement it as a generic mechanism with cross-domain tests, not as logic keyed to sample entities, sample wording, or a single benchmark corpus.
