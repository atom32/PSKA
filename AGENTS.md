## Project Workflow

- This project should not keep long-lived extra working branches for ordinary Codex changes.
- After a feature/fix is validated, merge it back into `master` and push `master`.
- When GitHub authentication is needed for push/merge operations, use the PAT stored at `~/Github_PAT.txt`; never write the token into the repository, logs, commit messages, or user-visible command output.
