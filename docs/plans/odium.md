# odium — design notes

**Bead:** odium-jha
**Status:** scaffolding / in progress

## Goal

Turn `geo` from a toolbox (which currently requires expert knowledge to
run) into a product a working drone surveyor can self-serve. The agent
walks the surveyor through a full job — from raw `.dc` file to
customer-deliverable orthophoto — asking questions, running each pipeline
stage, prompting for human-in-the-loop work (tagging in GCPEditorPro),
confirming expensive operations (EC2 cost), and moving data through its
lifecycle (local → S3 → NAS → archive).

**Two-platform scope.** A typical surveyor today depends on **Pix4D** for
production work and may use ODM as an alternative still being validated.
The agent should support **both** so the surveyor doesn't have to switch
tools mid-stream. This also makes the agent into a side-by-side
experimental harness: it can run the same job through ODM and Pix4D and
produce a comparison report, directly feeding the accuracy investigation
that's currently the main blocker on full ODM adoption.

The bigger picture: this is also a good Claude Agent SDK exercise for the
geo project, with reusable patterns for future agent work.

## Audience

A working drone surveyor: comfortable with a terminal but happier with a
simple GUI. Cross-platform: macOS *and* Windows. Doesn't write code,
doesn't run `npm start`, doesn't manage Python virtual environments.
Wants to do their job with as little ceremony as possible.

Important: the surveyor is NOT in Claude Code. They won't install Claude
Code. The agent is a standalone application that happens to use Claude
under the hood — Claude is plumbing, not the UI.

## Architecture choice

**Claude Agent SDK + lightweight local GUI.**

Briefly considered and rejected:
- **Claude Code skill** — wrong audience (assumes user is in Claude Code)
- **Claude Code subagent** — wrong invocation model (an LLM dispatches
  subagents, not a human)
- **MCP server alone** — engine without a UI; the surveyor still needs
  something to talk to. Could be a useful *layer* under the agent later,
  exposing the same tools to the developer's own Claude Code sessions,
  but not the starting point.
- **Raw Anthropic API** — gives full control, but the SDK absorbs the
  boilerplate around tool-use loops, context management, and persistence
  that we'd otherwise re-invent.

Layered structure if we want to expand later:

```
                 ┌────────────────────────────┐
                 │  Local GUI (surveyor-     │  ← what the surveyor sees
                 │  facing)                   │
                 └────────────┬───────────────┘
                              │
                 ┌────────────▼───────────────┐
                 │  Agent SDK loop            │  ← reasoning, tool use
                 │  (system prompt, tools,    │
                 │   confirmations, recovery) │
                 └────────────┬───────────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
       ┌──────▼─────┐  ┌──────▼──────┐  ┌─────▼──────┐
       │ transform.py│  │ sight.py    │  │ terraform  │
       │ rmse.py     │  │ packager.py │  │ + AWS      │
       └─────────────┘  └─────────────┘  └────────────┘
```

The agent is the orchestrator; the existing geo Python tools are the
worker bees, called as subprocesses.

## User journey sketch (MVP)

1. The surveyor double-clicks `odium`. A small window opens.
2. Agent: "Welcome. New job, or continue an existing one?"
3. New: asks for job name (e.g. `aztec8`), .dc file path (file picker),
   then confirms before running `transform.py dc`. Shows progress.
4. Asks where the Emlid CSV is once the field survey is done.
5. Asks where the drone images are.
6. Confirms before running `sight.py` (which can take a while on a
   large image set).
7. Says: "Now please tag the targets in GCPEditorPro. I'll launch it
   for you." Opens Chrome pointed at the local GCPEditorPro instance.
8. When the surveyor clicks "I'm done tagging" in the agent GUI, agent
   watches `~/Downloads/` for the tagged file, moves it to the job
   directory, runs `transform.py split`.
9. Cost confirmation: "Launching ODM on EC2 will cost ~$17 (20 hours
   @ \$0.85/hr). Hard ceiling for this job is \$30. Confirm?"
10. Runs terraform, monitors via SNS or polling.
11. Hours/days later (the surveyor may close and reopen the agent), it
    picks up where it left off, runs `rmse.py`, opens the HTML report
    in Chrome, runs `packager.py`, asks where on the NAS to put the
    deliverable, copies it.
12. Asks if the local job dir can be cleaned up now that the deliverable
    is on the NAS. Confirms, deletes.

## Pipeline state machine

Each step is a node; transitions are gated by either an automated check
or an explicit confirmation. The middle of the flow forks based on which
platform the job is using.

```
NEW
 ↓ (surveyor picks .dc; chooses platform: ODM, Pix4D, or both-for-comparison)
DC_PARSED              ─→ transform.py dc (NGS auto-lookup)
 ↓ (field survey done, surveyor picks Emlid CSV)
SURVEY_LOADED
 ↓ (surveyor picks images dir)
IMAGES_LOADED
 ↓ (surveyor confirms — sight.py is slow)
SIGHT_DONE             ─→ {job}.txt (ODM) + marks_design.csv (Pix4D)
 ↓
                  ┌─────────┴─────────┐
                  │                   │
              [ODM path]         [Pix4D path]
                  │                   │
                  ▼                   ▼
           TAGGING_ODM           TAGGING_PIX4D
           (GCPEditorPro)        (Pix4D Desktop, manual)
                  │                   │
                  ▼                   ▼
           TAGGED_ODM            TAGGED_PIX4D
                  │                   │
                  ▼                   ▼
           SPLIT_DONE            (Pix4D handles its own split)
                  │                   │
                  ▼ ($$$ confirm)     ▼
           EC2_LAUNCHED          PIX4D_RUNNING
                  │              (local or Pix4D Cloud)
                  ▼                   │
           ODM_COMPLETE          PIX4D_COMPLETE
                  │                   │
                  └─────────┬─────────┘
                            ▼
                      RMSE_DONE        (rmse.py, ODM input today;
                                        Pix4D input is geo-3ui future)
                            ▼
                      PACKAGED         (packager.py — works on either
                                        platform's orthophoto)
                            ▼ (surveyor picks NAS destination)
                      DELIVERED
                            ▼ (surveyor confirms cleanup)
                      ARCHIVED
```

State persists in `<job_dir>/.odium-state.json` so the surveyor can
close and reopen the agent without losing progress. The state file
records the platform(s) and the per-platform sub-state.

For **both-platforms-for-comparison** jobs, the two paths run in parallel
where possible (Pix4D locally while EC2 ODM is also running). At the
join point, the agent produces a side-by-side accuracy comparison
report — the same kind of comparison currently done manually for the
accuracy study, but automated.

## Multi-job awareness and data lifecycle

The agent isn't just a job runner — it's also a small data manager. It
should know:

- **Active jobs** — currently being worked on, full data on fast local disk
- **Delivered jobs** — done, local copy may be cleanup-eligible
- **Archived jobs** — only on NAS, references kept for "can you reprocess
  this job from 6 months ago" requests
- **S3 contents** — what jobs are still up there, are they needed for
  active runs, can old uploads be deleted

Concrete features:
- "List my jobs" → table of {name, status, local size, NAS status}
- "Clean up delivered jobs" → walks through, asks about each
- "Re-pull job X from NAS" → restores a previously-archived job to
  local disk for re-processing or revisiting
- "What's in S3?" → shows S3 contents with a "delete unreferenced" option

Storage tiers:

| Tier | Speed | Use |
|---|---|---|
| **Fast local SSD** | ms | Active job working dir |
| **NAS** | tens of ms | Delivered job archive, post-delivery backup |
| **S3** | seconds | EC2 staging only — drops files in/out during ODM run |

Lifecycle: local ↔ S3 (during run) → local + NAS (after delivery) → NAS only
(after surveyor confirms cleanup).

The data-lifecycle features are **platform-agnostic** — they apply equally
to ODM jobs, Pix4D jobs, and comparison jobs. This is part of why
supporting both platforms in the same agent is a clean fit, not a tacked-on
extra.

## Open design questions

These need resolution before scaffolding starts.

### 1. GUI framework

Options ranked by current bias:

| Option | Pros | Cons |
|---|---|---|
| **Python + Flask + browser** (like packager/app.py) | Trivial to ship, cross-platform, no compile step, existing precedent in `packager/` | Browser feels web-y; no native menus/tray |
| **Tauri** (Rust + webview) | Small bundle, native feel, modern | Rust learning curve; Windows packaging less mature |
| **Electron** (Node + webview) | Most cross-platform; well-trodden path | Heavy bundle (~150 MB); Node toolchain |
| **Python + Qt/Tk** | Pure Python, native widgets | Ugly Tk, complex Qt licensing on macOS |

**Tentative pick: Python + Flask + browser**, served on `localhost:5002`,
auto-opens Chrome on launch. Same approach as `packager/app.py`. Lowest
build complexity. The "GUI" is HTML/CSS/JS. Easy to ship as a single
Python file or PyInstaller bundle.

### 2. Anthropic API key

Where does it come from?

| Option | Pros | Cons |
|---|---|---|
| **Surveyor signs up for their own** | Clean cost separation; surveyor owns it | Friction during first-run setup |
| **Maintainer provisions a key** | Zero setup; maintainer controls the cost ceiling | Maintainer eats the cost; key embedded in distributable is a security concern |
| **Anthropic Bedrock via the surveyor's AWS** | Cost flows through the surveyor's AWS bill (where the EC2 cost already lives) | Bedrock setup is non-trivial; latency higher |
| **Pre-paid credit** | Maintainer hands over a pre-funded key, expires when balance runs out | Operational headache |

**Tentative pick: surveyor signs up for their own**, agent walks them
through the signup as part of first-run config. Once-per-installation
friction.

### 3. AWS credential bootstrap

"Agent helps the surveyor get their own AWS credentials, then uses them
for jobs" — what does that look like concretely?

- **First-run wizard**: agent detects no `~/.aws/credentials`, opens
  Chrome to console.aws.amazon.com signup, walks the surveyor through
  creating an IAM user with the necessary policies (S3 read/write to
  their own bucket, EC2 launch/describe/terminate, terraform's required
  perms).
- **IAM policies**: a minimal "geo-pipeline-user" policy needs to be
  defined and shipped with the agent. JSON file, applied via `aws iam
  create-user/put-user-policy` or the console.
- **Credentials storage**: standard `~/.aws/credentials` profile so
  terraform and aws-cli pick them up automatically.

Open: does each surveyor need their own S3 bucket, or share a maintainer
bucket? Latter is simpler but conflates billing. Probably their own
bucket per deployment.

### 4. GCPEditorPro packaging

Currently the workflow does `cd ~/git/GCPEditorPro && npm start`.
A surveyor won't have Node, won't have the fork's source tree. Options:

| Option | Pros | Cons |
|---|---|---|
| **Use upstream Electron release** | One installer, works out of the box | Doesn't have the fork's pipeline-aware features |
| **Build the fork as Electron** | Has all the features needed | Electron packaging, sign for macOS, certify for Windows |
| **Bundle as a docker container** | Isolated, reproducible | Requires Docker on the surveyor's machine; macOS/Windows Docker is heavy |
| **Static-build the Angular app, serve from Flask** | No Node needed at runtime — the agent serves it | Build pipeline to manage; updates need a re-build |

**Tentative pick: static-build the Angular app and serve from the
agent's Flask process**, on a different port (`localhost:4200` matches
dev default). The agent owns both the agent UI and the GCPEditorPro UI.

### 5. NAS integration

- What protocol? SMB (Windows-friendly) or NFS (Mac-friendly) or rsync
  to a remote host?
- Where does the agent learn about the NAS? First-run config:
  "Where should I back up your jobs? (path to a mounted share)"
- Mounted vs network: easier to assume the NAS is mounted as a regular
  filesystem path (`/Volumes/NAS/geo` on macOS, `Z:\geo` on Windows).

**Tentative pick: assume NAS is already mounted by the OS**, agent
stores the path in its config and treats it as a regular directory.

### 6. Cost ceiling enforcement

- **Per-job hard ceiling**: configurable, default $30. Agent refuses to
  proceed past the EC2 launch if the projected cost exceeds the ceiling.
- **Daily ceiling**: optional second tier, prevents accidentally running
  10 jobs in one day. Default $100/day.
- **Live monitoring**: while ODM is running, agent polls AWS pricing API
  + elapsed time; if a job overruns its projection (instance auto-resume
  after spot interruption can stretch runtime), agent warns the surveyor.

### 7. Failure mode and human escalation

- **MVP**: error is shown to the surveyor, instruction is "contact your
  maintainer" with a clearly displayed contact, agent preserves all
  state and logs in the job dir for diagnosis.
- **Stretch**: agent emails or texts the maintainer directly with the
  failure context (this is just an SES/SNS call from the agent).
- **Stretch**: agent has an "ask maintainer" tool that captures the
  surveyor's question + relevant logs and sends them, who can reply.

### 8. Pix4D integration specifics

This is the most under-defined area.

- **Pix4D CLI** — does Pix4D have a batch/CLI interface, or is it strictly
  GUI? If GUI-only, the agent's role is more like "open Pix4D to the
  right project and wait for the surveyor to finish" rather than
  "automate the run".
- **Pix4D Desktop vs Pix4D Cloud** — which one does the surveyor use?
  Different integration paths.
- **Pix4D project file format** — what does the agent need to produce
  to bootstrap a Pix4D project from sight.py's output?
- **`marks_design.csv` already exists** — sight.py emits this in the
  Pix4D-expected format. The integration question is mostly about
  *handing it off* to Pix4D and *picking up the result*.
- **rmse.py for Pix4D inputs** — bead **geo-3ui** in the backlog
  ("rmse.py: support Pix4D inputs for cross-platform accuracy
  comparison"). Is a prerequisite for the Pix4D side of the
  comparison harness.
- **Output file conventions** — Pix4D writes its orthophoto in a
  different layout than ODM. The packager would need to handle both,
  or have a Pix4D-specific entry point.

These are all open and deserve a dedicated investigation pass before
the Pix4D path goes beyond "stub" in the MVP.

### 9. Multi-job state file location

- **Per-job state**: lives in the job dir as `.odium-state.json`.
  This is the resumption fuel.
- **Global registry**: a small index in `~/.odium/jobs.json` listing
  all known jobs and their statuses. Used for "list my jobs", lifecycle
  management, NAS sync tracking.

### 10. The agent's tool definitions

Probably one tool per pipeline stage, plus a few utilities:

- `transform_dc(dc_path, job_name, anchor_override=None)`
- `parse_emlid_csv(csv_path)` (validation only — returns target count, CRS)
- `run_sight(survey_csv, images_dir, output_dir)`
- `wait_for_tagging(downloads_dir, expected_filename)` — watches the
  filesystem
- `transform_split(tagged_path, output_dir)`
- `upload_to_s3(job_dir, s3_prefix)`
- `terraform_apply(project, notify_email)` — also estimates cost first
- `monitor_ec2(instance_id)` — checks SNS/CloudWatch for stage events
- `download_results(s3_prefix, job_dir)`
- `run_rmse(job_dir)` — produces HTML report
- `run_packager(job_dir, transform_yaml)`
- `copy_to_nas(job_dir, nas_path)`
- `archive_local(job_dir)` — removes after confirmation
- `list_jobs()`, `cleanup_s3()`, `restore_from_nas()` — multi-job ops
- `escalate_to_maintainer(question, context)` — failure mode
- `(future)` Pix4D-side tools: `launch_pix4d_project`, `import_pix4d_results`

### 11. Conversation persistence

The Agent SDK manages conversation context; we need to decide how that
relates to the job dir. Two options:

- **Per-job conversation** — each job dir has its own conversation
  history, resumed when the surveyor picks the job back up. Cleaner.
- **Global conversation** — one ongoing conversation across all jobs.
  Lets the surveyor ask "what was the GCP RMS_H on the aztec8 job last
  month".

Tentative: **per-job conversation as primary**, with a small
"meta-agent" mode that can reference past jobs via the global registry.

## MVP scope

The smallest thing that's actually useful:

- New-job flow only (no resume across sessions yet)
- **ODM path only** — Pix4D support deferred to v2 (after question 8 is
  worked out)
- All steps from `transform.py dc` through `packager.py`
- File pickers for .dc, Emlid CSV, images dir
- Cost confirmation before EC2 launch
- Hard ceiling enforcement
- Failure → "contact maintainer" with state preserved
- Static-built GCPEditorPro served from same Flask process
- AWS credentials assumed already configured (skip the bootstrap wizard)
- Local-only output (no NAS copying yet)
- One ongoing conversation, no global registry

## Future scope

Once the MVP is working with a real surveyor:

- Resume-across-sessions
- AWS credentials bootstrap wizard
- **Pix4D path** (after question 8 is settled)
- **Side-by-side comparison harness** (run a job through both platforms,
  produce a comparison report — the natural next step once Pix4D is in)
- NAS integration + lifecycle management
- Multi-job registry and "list jobs" / "clean up" commands
- Stretch: agent escalates to the maintainer via SNS/email
- Stretch: web-app version accessible from anywhere
- Stretch: meta-agent mode for cross-job queries

## Risks

| Risk | Mitigation |
|---|---|
| **Token cost runs away** during a complex conversation | Use Sonnet not Opus; cap conversation length; report token usage after each session |
| **Tool calls are wrong** (agent confuses paths, parameters) | Strong validation in each tool; explicit confirmation before destructive operations |
| **GCPEditorPro static build drifts** from the source repo | Build pipeline that re-bundles on any commit to the fork; agent version embeds GCPEditorPro version |
| **AWS credentials leak** if maintainer-provisioned | Don't ship credentials; surveyor has their own |
| **macOS/Windows divergence** in file paths, NAS mounts, browser launch | Test on both early; use `pathlib` and `webbrowser` everywhere |
| **Spot interruption recovery** during long ODM runs surfaces a state the surveyor can't interpret | Agent monitors and translates the SNS notifications into plain English; "you don't need to do anything" messaging |
| **Pix4D integration is much harder than expected** | Defer to v2; ship ODM-only MVP first |
| **The thing is too narrow to justify the build cost** if surveyor only runs 2 jobs/year | Validate demand first (talk to the actual user explicitly about expected job frequency before building) |

## Effort estimate (rough)

- **State-machine spec doc**: this doc is the spec. Done after questions
  are answered.
- **Tool definitions + system prompt**: 1–2 days
- **Flask GUI**: 2–3 days
- **End-to-end ODM-only MVP**: ~1 week of focused work
- **Polish + testing on a real surveyor's job**: another week
- **Pix4D path**: variable depending on question 8 answers; budget 1–2
  weeks if Pix4D has a CLI, 2–3 if it's GUI-only with file watching
- **NAS / multi-job / lifecycle features**: another 1–2 weeks
- **Total to "a surveyor can run a job themselves"** (ODM-only): ~2 weeks
- **Total to "surveyor loves it"** (incl. Pix4D + lifecycle): ~6 weeks

## Resolved design decisions (2026-04-14)

### Tone and personality
Friendly, helpful, knowledgeable. Mostly get-it-done, but narrate actions
as they happen: "converting from EPSG:6529 to 32613, estimating tag
positions, ready for you to tag at http://..." — the surveyor should
always know what's happening and why.

### Agent capability level
Capable agent, not script-follower. Expected challenge areas where the
agent needs real smarts:
- **CRS/design shifts** — transform.py has some logic but probably
  insufficient for all cases; agent may need to reason about coordinate
  systems
- **ODM runs** — historically require significant troubleshooting even
  on-demand; especially complex when switching instance types per stage
- **Point cloud issues** — misclassifications, artifacts; agent helps
  the surveyor get into tools like QGIS (bead geo-7znc), CloudCompare,
  etc.

### State management
- Explicit state file per job (`.odium-state.json`)
- Do NOT gate allowed actions — instead, warn about consequences:
  "You're at step E. Going back to step B means re-doing B→E, which
  will cost ~$Y and take ~N hours. Confirm?"
- State must be resilient: restartable across sessions, portable across
  machines (state can be synced up/down via S3 to move between computers)

### Confirmation policy
- **Time**: advise with estimates, don't block. Track historical runtimes
  (system, image count, elapsed time) so estimates improve with use.
- **Money**: confirm before spending (EC2 launch, significant rework that
  re-incurs AWS cost)
- **Destructive**: confirm before cleanup, archive, overwrites
- **Rework**: confirm if significant cost or time involved

### Error handling
- Depends on error type — no blanket policy
- AWS tooling should be smart enough to shepherd jobs to completion
  (retry transient failures, handle spot interruptions)
- Agent should know how to check AWS run status (CloudWatch, SNS,
  SSH to instance) — same diagnostic patterns used during aztec2–7 runs
- When expectations are disappointed, agent helps diagnose and recover

### Infrastructure decisions
- **Repo**: separate repo (`odium`), geo is a runtime dependency
- **Env**: separate conda env (`odium`), calls geo tools via
  `conda run -n geo python ...`
- **GUI**: Flask + browser (MVP), same pattern as packager/app.py
- **API key**: surveyor signs up for their own (agent walks through setup)
- **AWS**: each surveyor has their own AWS account; dev/test uses
  maintainer's account
- **Pix4D**: deferred to v2 (ODM-only MVP first)

### EC2/ODM decisions (2026-04-15)

**Naming**: use "job" consistently throughout (not "project"). Already in
workflow diagram, transform.yaml, state file. Grafana dashboards should
use `job` tag. S3 prefix is `{client}/{job}`.

**Terraform**: odium shells out to terraform in `geo/infra/ec2/`. Terraform
manages the AWS resources; odium manages the workflow around them.
Odium exports `TF_VAR_*` from `~/.odium/env` before running terraform.

**Per-job AWS budget**: each job gets an AWS Budget on launch, tagged with
the job name. Budget = user-confirmed ceiling (e.g. $30). AWS enforces
it (auto-stop instances at threshold). Not a global first-run budget —
created per job at launch time.

**Email notifications**: required unless user explicitly opts out. Agent
asks for email, stores in `~/.odium/env`. On launch: "I'll send status
updates to you@email.com. ODM will take ~4 hours. Proceed?"

**Multiple concurrent jobs**: each job is independent — own S3 prefix,
own EC2 instance, own budget. Odium doesn't block parallel runs; helps
the user manage them if they want multiple.

**S3 download**: only what's needed for deliverables + RMSE, not
everything. Specifically:
- `opensfm/reconstruction.topocentric.json` — for RMSE 6a
- `odm_orthophoto/` — orthophoto for RMSE + packaging
- `cameras.json` — for future sight.py re-runs
- NOT: undistorted images, point clouds, DEM (download on demand if needed)

**Cost guardrails (three layers)**:
1. **AWS Budget** (enforced, per job): auto-stop instances at ceiling.
   Created by odium at launch time with job tag.
2. **Pre-launch estimate**: image count × historical rate → estimated
   cost + 1.5x safety margin. Shown to user for confirmation.
3. **Runtime monitor**: track elapsed time vs estimate. Alert if 2x
   over. Optional hard wall-clock timeout.

**Data lifecycle**: tracked in odium-r50 (deferred). Per-job inventory
of data across local/NAS/S3 with cost tracking and archive assistance.

## Next steps

1. ~~Validate demand~~ — proceeding; secondary goal is Agent SDK learning
2. ~~Resolve open questions 1–11~~ — key decisions resolved above;
   Q8 (Pix4D) deferred to v2
3. ~~Scaffold the project layout~~ — done: `~/git/odium/` separate repo
4. ~~Write the system prompt~~ — done (iterating)
5. ~~API credits~~ — working ($5, Haiku default)
6. ~~Build the agent loop~~ — done (17 tools, terminal-first)
7. ~~Add real tool implementations~~ — done for local tools
8. **Wire EC2/ODM** — S3 sync, terraform apply/destroy, monitoring
9. **Add Flask GUI** — presentation layer over the working agent
