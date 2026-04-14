# odium — ODM pipeline orchestrator agent

## What this is
A Claude Agent SDK application that walks a drone surveyor through the geo pipeline.
Flask GUI served on localhost. The `geo` repo (`~/git/geo`) contains the underlying
Python tools (transform.py, sight.py, rmse.py, packager.py) that odium orchestrates.

## Permissions
- All local file reads are pre-approved (Read, Glob, Grep, cat, head, etc.)
- All git read commands are pre-approved
- All read-only Bash commands are pre-approved
- `bd` (beads) commands are pre-approved

## Conventions
- Python 3.11+, no type: ignore unless unavoidable
- Agent SDK for the orchestration loop; Flask for the GUI
- geo tools called as subprocesses (not imported)
- geo repo is a runtime dependency, not a build dependency

## Testing
- Do NOT commit until testing is confirmed by the user
- After confirmation: bd sync -> git commit -> git push
