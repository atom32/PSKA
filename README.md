# PSKA

Personal Social Knowledge Archive.

This repository is being organized as a PSKA workspace. The current implemented
component is the Twitter/X acquisition channel:

```text
channels/twitter-x/
```

That channel contains:

- Chrome extension for archiving from a logged-in Chrome session
- Python CLI collector prototype
- PSKA archive metadata schema documentation
- Tests for URL parsing and archive output

Future PSKA core work can live at the repository root or under dedicated folders
such as `core/`, `apps/`, `schemas/`, and additional `channels/`.
