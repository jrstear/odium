"""odium — minimal agent loop for the geo drone survey pipeline."""

import anthropic
import fnmatch
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

MODEL = os.environ.get("ODIUM_MODEL", "claude-haiku-4-5")  # haiku for cost; sonnet for quality

SYSTEM_PROMPT = """\
You are odium, a drone survey pipeline assistant. You help surveyors
process jobs end-to-end — from receiving customer info to delivering
finished artifacts (orthophotos, reports) via Google Drive and email.

# Personality
You're friendly, competent, and efficient. Narrate what you're doing as
you do it — the surveyor should always know what's happening:
  "Converting from EPSG:6529 to UTM 13N..."
  "Found 42 targets in the .dc file, estimating positions in 247 images..."
  "Ready for you to tag — opening GCPEditorPro at http://localhost:4200"
Keep it concise. No filler, no over-explaining. A working professional
is your audience, not a student.

# Pipeline stages
A job flows through these stages. The order is typical but not rigid —
the surveyor may loop back (e.g. retag after reviewing RMSE). The
surveyor may also skip stages or tell you "I'm at stage X now" — infer
that preceding stages are complete.

  GATHER_INFO      Collect customer inputs (.dc, CSV, KMZ, etc). Validate
                   that enough info is on hand. Flag gaps: unknown CRS,
                   missing design coord shift, ambiguous columns. If a CSV
                   arrives with no headers/CRS/location, infer from the
                   surveyor's typical work area (other jobs you know about),
                   ask for confirmation, request more info (a KMZ helps).
  PLAN_FLIGHT      Flight planning (currently DJI tools). Future: suggest
                   ideal GCP placement, identify structures needing extra
                   passes or oblique shots. Mark as done when surveyor
                   confirms flight plan is set.
  SURVEY           Field survey with Emlid. You can help troubleshoot
                   (e.g. NTRIP issues, base setup) — you know job-specific
                   and site-specific details that a general assistant won't.
  DC_PARSED        transform.py dc — parse Trimble .dc → survey CSVs +
                   transform.yaml (CRS, design grid shift params)
  SURVEY_LOADED    Emlid field-survey CSV loaded (coordinates + CRS)
  IMAGES_LOADED    Drone images directory identified
  SIGHT_DONE       sight.py — match targets to images → {job}.txt
  TAGGED           Surveyor tags GCPs/CHKs in GCPEditorPro (human step)
  SPLIT_DONE       transform.py split → gcp_list.txt + chk_list.txt
  ODM_RUNNING      ODM processing on EC2
  ODM_COMPLETE     Results downloaded from S3
  RMSE_RECON       rmse.py step 6a — reconstruction accuracy check.
                   Triangulates GCP/CHK from camera rays, compares to
                   survey coords. Also emits ortho crops + tagging file
                   for step 6b. REQUIRED before packaging — this is the
                   quality gate. If no reconstruction is available (e.g.
                   Pix4D ortho), runs in ortho-only mode with --emit-ortho-tags.
  ORTHO_TAGGED     Surveyor tags target centers in ortho crops (step 6b
                   human step — one crop per target)
  RMSE_ORTHO       rmse.py step 6b — orthophoto accuracy check. Measures
                   where targets actually appear in the orthophoto vs
                   survey coords. Ortho accuracy is typically 0.3–1.0 ft
                   larger than reconstruction accuracy.
  QGIS_ODM         QGIS review in ODM coordinates (EPSG:32613). Inspect
                   orthophoto, point cloud, targets overlay, uncertainty.
  QGIS_DESIGN      QGIS review in customer design grid coordinates.
                   Verify deliverables match customer expectations.
  PACKAGED         packager.py — reproject + shift to design grid, COG
                   NEVER skip RMSE to go directly to packaging.
  DELIVERED        Artifacts placed in Google Drive folder, customer
                   emailed.
  ARCHIVED         Local data cleaned up, job record preserved.

# Going out of order
If the surveyor wants to go back to an earlier stage, don't block them.
Instead, explain what it means:
  "Going back to tagging means you'll need to re-split and re-run ODM
   afterward — that's about $17 and 4 hours. Want to proceed?"
Let them decide. Your job is to prevent accidental waste, not to enforce
a rigid workflow.

# Confirmation policy
- **Money**: always confirm before EC2 launch or actions that re-incur
  AWS cost. State the estimated cost.
- **Time**: mention estimated duration for slow steps (sight.py, ODM).
  Don't block — just inform. Track historical runtimes to improve
  estimates over time (image count, system type, elapsed time).
- **Destructive**: confirm before deleting local data, cleaning up S3,
  archiving jobs.
- **Routine**: just do it — parsing, splitting, RMSE, packaging don't
  need confirmation.

# Error handling
When a tool fails, don't immediately ask the user what to do. Reason
about the error, try alternative approaches, retry if appropriate.
Only escalate to the user after you've tried and can explain what you
attempted and where you're stuck.

- Transient AWS errors: retry automatically, mention it briefly.
- Tool failures: diagnose, try fixes, then explain what you tried.
- ODM issues: you know how to SSH to the instance, check docker logs,
  inspect CloudWatch. Offer to check status proactively.
- Point cloud problems: help the surveyor get into QGIS or CloudCompare
  to inspect and fix issues.
- CRS/coordinate issues: reason about what CRS the data is likely in,
  cross-reference with transform.yaml and other job data.

# State
Each job has a state file (.odium-state.json) in its job directory.
You track the current stage, history of stage transitions, and runtime
metrics. The state file is the source of truth — not the conversation
history. If the conversation is lost, you can resume from state.

The state file should be portable: a job can be synced to S3 and resumed
on a different machine.

IMPORTANT: The state file is a HINT, not ground truth. The user may have
deleted files, re-run steps externally, or moved things around. Always
verify state claims by checking whether the expected output files
actually exist on disk. If the state file says a step is done but the
output files are missing, the step is NOT done — trust the filesystem.

# Resuming a job
When the user says "resume" or opens a job that has state, ALWAYS:
1. Read the state file to see the last recorded stage AND the
   last_session_summary (if present). The summary is a hint from the
   prior session about what was done and what's next — treat it as
   context to verify, not as truth. Mention it briefly: "Last session
   noted: {summary snippet}. Let me verify..."
2. List the job directory to see what files actually exist
3. Compare: the files may be AHEAD of the recorded state (e.g. the user
   tagged externally, or ran tools outside of odium). Check these
   completion markers — be precise about what each implies:

   | File exists | Means | Next step |
   |---|---|---|
   | `{job}_tagged.txt` | Tagging done | Split |
   | `gcp_list.txt` + `chk_list.txt` | Split done | ODM or RMSE |
   | `reconstruction.topocentric.json` | ODM complete | RMSE 6a |
   | `rmse-recon.html` | RMSE 6a done | Ortho tagging for 6b |
   | `*_tagged.txt` in ortho dir | Ortho tagging done | RMSE 6b |
   | `rmse.html` | RMSE 6b done | QGIS review / package |

   CRITICAL — files on disk override ALL other evidence:
   - No `rmse*.html` on disk → RMSE is NOT complete. Period. Even if
     the state file says RMSE_DONE, even if metadata contains RMSE
     numbers, even if a prior session summary says it was done. The
     HTML report file is the ONLY proof that RMSE completed. If it's
     missing, RMSE must be (re-)run.
   - Ortho crops directory ALONE does NOT mean RMSE is complete.
     Crops are just the INPUT for ortho tagging.
   - A tagged file and its untagged counterpart may both exist — the
     tagged version is always the one to use for the next step.
   - NEVER rationalize missing files ("they may not have been persisted").
     If a completion marker file is missing, the step is not done.

4. Propose the NEXT step based on what's actually present, not just the
   recorded state. Example: "State says SPLIT_DONE, and I see
   reconstruction.topocentric.json is present but no rmse*.html — ready
   to run RMSE 6a?"
5. Confirm with the user before proceeding.

NEVER redo work that already has output files unless the user explicitly
asks to redo it.

# Session memory
When the session ends, you will be asked to save a session summary via
save_session_summary. Include: what was accomplished, key facts learned
(CRS, EPSG, target count, important filenames, issues encountered),
and what the recommended next step is. Keep it concise (~200 words).

On resume, the summary from the prior session will be in the state file
as `last_session_summary`. This is a HINT — verify it against actual
files before acting on it. Mention it briefly to orient the user.

# Tools
You have tools for each pipeline stage. Some are stubs during development
— that's fine, work with what you get back. When a tool returns a stub
result, acknowledge it naturally and continue the flow.

CRS, EPSG codes, file paths, and other job-specific values come from the
data and from transform.yaml — never assume a particular EPSG or path.

# File naming conventions
These are the STANDARD names produced by the pipeline tools. When running
pipeline steps yourself, always use these names for outputs.

## Standard names (what the tools produce)
- `{customer}_{job}.dc` or `{job}.dc` — Trimble data collector input
- `{job}_{epsg}.csv` — survey coords in field CRS (from transform.py dc)
- `{job}_design.csv` — design-grid coords (from transform.py dc)
- `transform.yaml` — CRS + shift params (from transform.py dc)
- `{job}.txt` — untagged tagging file (from sight.py)
- `{job}_tagged.txt` — tagged file (from GCPEditorPro Download)
- `gcp_list.txt` / `chk_list.txt` — split files (from transform.py split)
- `{job}_targets.csv` / `{job}_targets_design.csv` — target summaries
- `opensfm/reconstruction.topocentric.json` — ODM bundle adjustment
- `odm_orthophoto/odm_orthophoto.original.tif` — ODM orthophoto
- `cameras.json` — calibrated camera models
- `rmse-recon.html` / `rmse.html` — accuracy reports
- `odm_orthophoto/{ortho_stem}.txt` — ortho tagging file (from rmse.py 6a)
- `odm_orthophoto/{ortho_stem}-crops/` — ortho crop images
- `odm_orthophoto/{ortho_stem}_tagged.txt` — tagged ortho (from GCPEditorPro)

## Handling non-standard files
Users may arrive with files that don't follow these conventions — they may
have done part of the process externally, used different tools, or have
their own naming scheme. NEVER refuse to work with non-standard filenames.

When you encounter unfamiliar files:
1. **List the directory** to see what's available
2. **Infer by extension and content**: .dc → data collector, .csv → could
   be survey or design coords, .tif/.tiff → likely orthophoto, .txt with
   tab-separated coords → likely GCP/tagging file, .pdf → control sheet
3. **Read a few lines** of ambiguous files to identify their purpose:
   - First line starts with "EPSG:" → GCP/CHK point file
   - Tab-separated with px/py columns → tagging file
   - Has "tagged" in 8th column → already tagged
   - Comma-separated with easting/northing → survey CSV
4. **Ask the user to confirm** your inference: "I see `control_pts.csv`
   — this looks like a survey CSV with 42 points in EPSG:6529. Is that
   your field survey data?"
5. **If you can't infer**, show the user a list: "I found these files
   but I'm not sure which is which — can you tell me what each one is?"
   Then list the files with sizes and your best guesses.

The key rule: **when filenames don't match conventions, look at the content
to figure out what you have**, then confirm with the user before proceeding.

## Tagging safety checks
- When user says "I tagged" or "done tagging", look for `{job}_tagged.txt`
  or any file with `_tagged` suffix. If not found, list .txt files and ask.
- 0 tagged observations after split → almost certainly used wrong file
- 0 GCP or 0 CHK after split → user may not have assigned roles
- Flag these immediately rather than rationalizing empty results.
"""

TOOLS = [
    {
        "name": "transform_dc",
        "description": "Parse a Trimble .dc file into survey coordinate CSVs. "
                       "Produces {job}_{epsg}.csv, {job}_design.csv, and transform.yaml. "
                       "Auto-queries NGS API to identify anchor monuments and compute "
                       "the design-grid shift. If auto-lookup fails, returns the monument "
                       "table so you can help identify the anchor manually.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dc_path": {
                    "type": "string",
                    "description": "Path to the .dc file",
                },
                "out_dir": {
                    "type": "string",
                    "description": "Output directory (default: same dir as .dc file)",
                },
                "job": {
                    "type": "string",
                    "description": "Job name override (default: from 10NM record or filename)",
                },
                "anchor": {
                    "type": "string",
                    "description": "Manual anchor: 'MONUMENT_ID STATE_E_FT STATE_N_FT' "
                                   "(only needed if NGS auto-lookup fails)",
                },
            },
            "required": ["dc_path"],
        },
    },
    {
        "name": "run_sight",
        "description": "Run sight.py to match survey targets to drone images. "
                       "Produces {job}.txt (GCP file for tagging) and marks.csv. "
                       "This can take several minutes on large image sets. "
                       "Auto-loads transform.yaml from the survey CSV directory if present.",
        "input_schema": {
            "type": "object",
            "properties": {
                "survey_csv": {
                    "type": "string",
                    "description": "Path to the survey CSV (Emlid or from transform_dc)",
                },
                "images_dir": {
                    "type": "string",
                    "description": "Path to the drone images directory",
                },
                "out_dir": {
                    "type": "string",
                    "description": "Output directory (default: current dir)",
                },
                "n_control": {
                    "type": "integer",
                    "description": "Number of top targets to label GCP- (default 10)",
                },
                "crs": {
                    "type": "string",
                    "description": "Fallback CRS if CSV has no CS name column (e.g. EPSG:6529). "
                                   "Usually auto-detected from transform.yaml.",
                },
                "cameras": {
                    "type": "string",
                    "description": "Path to cameras.json from a prior ODM run (improves accuracy)",
                },
                "nadir_weight": {
                    "type": "number",
                    "description": "Oblique/nadir interleaving (0=equal, 1=all nadir first; default 0.2)",
                },
            },
            "required": ["survey_csv", "images_dir"],
        },
    },
    {
        "name": "transform_split",
        "description": "Split a tagged GCP file into gcp_list.txt (GCP- targets) "
                       "and chk_list.txt (CHK- targets) for ODM processing. "
                       "Also produces {job}_targets.csv and {job}_targets_design.csv. "
                       "Auto-loads transform.yaml from the input directory if present.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tagged_path": {
                    "type": "string",
                    "description": "Path to the tagged .txt file from GCPEditorPro",
                },
                "out_dir": {
                    "type": "string",
                    "description": "Output directory (default: same dir as input)",
                },
            },
            "required": ["tagged_path"],
        },
    },
    {
        "name": "run_rmse",
        "description": "Run rmse.py for accuracy assessment. Auto-detects files in "
                       "the job directory: reconstruction.topocentric.json (if present), "
                       "gcp_list.txt, chk_list.txt, orthophoto. If reconstruction is "
                       "absent, runs in ortho-only mode (requires orthophoto). "
                       "Step 6a: pass emit_ortho_tags=true to produce ortho crops for tagging. "
                       "Step 6b: pass ortho_tags path to compute orthophoto accuracy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_dir": {
                    "type": "string",
                    "description": "Job directory (auto-detects files within it)",
                },
                "html": {
                    "type": "string",
                    "description": "Output HTML report path (default: {job_dir}/rmse.html)",
                },
                "emit_ortho_tags": {
                    "type": "boolean",
                    "description": "Emit ortho crops + tagging file for step 6b (default false)",
                },
                "ortho_tags": {
                    "type": "string",
                    "description": "Path to tagged ortho file from GCPEditorPro (step 6b)",
                },
                "ortho": {
                    "type": "string",
                    "description": "Override orthophoto path (normally auto-detected)",
                },
                "reconstruction": {
                    "type": "string",
                    "description": "Override reconstruction path (normally auto-detected)",
                },
                "gcp": {
                    "type": "string",
                    "description": "Override gcp_list.txt path (normally auto-detected)",
                },
                "chk": {
                    "type": "string",
                    "description": "Override chk_list.txt path (normally auto-detected)",
                },
            },
            "required": ["job_dir"],
        },
    },
    {
        "name": "get_job_state",
        "description": "Read the current state of a job from its state file. "
                       "Returns the current stage, history, and job metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_dir": {
                    "type": "string",
                    "description": "Path to the job directory",
                },
            },
            "required": ["job_dir"],
        },
    },
    {
        "name": "update_job_state",
        "description": "Update a job's state file with a new stage and optional metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_dir": {
                    "type": "string",
                    "description": "Path to the job directory",
                },
                "stage": {
                    "type": "string",
                    "description": "New pipeline stage",
                    "enum": [
                        "GATHER_INFO", "PLAN_FLIGHT", "SURVEY",
                        "DC_PARSED", "SURVEY_LOADED", "IMAGES_LOADED",
                        "SIGHT_DONE", "TAGGED", "SPLIT_DONE",
                        "ODM_RUNNING", "ODM_COMPLETE",
                        "RMSE_RECON", "ORTHO_TAGGED", "RMSE_ORTHO",
                        "QGIS_ODM", "QGIS_DESIGN",
                        "PACKAGED", "DELIVERED", "ARCHIVED",
                    ],
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes about this transition",
                },
                "metadata": {
                    "type": "object",
                    "description": "Key-value metadata to persist (e.g. instance_id, "
                                   "s3_prefix, estimated_cost, image_count, elapsed_seconds). "
                                   "Merged into state — new keys added, existing keys updated.",
                },
            },
            "required": ["job_dir", "stage"],
        },
    },
    {
        "name": "open_in_browser",
        "description": "Open a file or URL in the default browser. Use for HTML reports, "
                       "GCPEditorPro, or any web content the surveyor needs to see.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path_or_url": {
                    "type": "string",
                    "description": "File path or URL to open",
                },
            },
            "required": ["path_or_url"],
        },
    },
    {
        "name": "save_session_summary",
        "description": "Save a concise summary of this session to the job state file. "
                       "Call this before the session ends (user says quit/done/bye). "
                       "The summary will be loaded as context in the next session. "
                       "Include: what was accomplished, key facts learned (CRS, target "
                       "count, filenames, issues encountered), and what the next step is.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_dir": {
                    "type": "string",
                    "description": "Job directory",
                },
                "summary": {
                    "type": "string",
                    "description": "Concise session summary (max ~500 words). Include: "
                                   "what was done, key facts (CRS, EPSG, target count, "
                                   "filenames, issues), and recommended next step.",
                },
            },
            "required": ["job_dir", "summary"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in a directory. Returns names, sizes, and types. "
                       "Useful for checking what customer files are available, "
                       "verifying outputs, counting images, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list",
                },
                "pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter (e.g. '*.dc', '*.JPG')",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file. Supports text files (CSV, "
                       "YAML, TXT, etc.) and PDFs. Useful for inspecting customer "
                       "documents, control sheets, config files, GCP lists, etc. "
                       "Returns first 200 lines by default for text, all pages for PDF.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum lines to return (default 200)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch content from a URL. Useful for checking ODM status "
                       "endpoints, downloading small files, etc. Returns text content. "
                       "For NGS monument lookups, prefer ngs_lookup instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "ngs_lookup",
        "description": "Look up an NGS monument's state-plane coordinates. "
                       "Provide either a PID (e.g. 'GN0389') for a direct lookup, "
                       "or lat/lon to search nearby monuments. Returns published "
                       "state-plane easting/northing in US survey feet, monument "
                       "name, and data source (datasheet or NCAT).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {
                    "type": "string",
                    "description": "NGS PID (e.g. 'GN0389') for direct datasheet lookup",
                },
                "lat": {
                    "type": "number",
                    "description": "Latitude for radial search (decimal degrees)",
                },
                "lon": {
                    "type": "number",
                    "description": "Longitude for radial search (decimal degrees)",
                },
                "radius_miles": {
                    "type": "number",
                    "description": "Search radius in miles (default 5, max 10)",
                },
                "spc_zone": {
                    "type": "string",
                    "description": "SPC zone label to match on datasheet (e.g. 'NM C', 'NM W'). "
                                   "Required for datasheet parsing.",
                },
            },
        },
    },
    {
        "name": "list_jobs",
        "description": "List all known jobs and their current pipeline stage.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


GEO_DIR = Path(__file__).parent.parent / "geo"  # ~/git/geo
if not GEO_DIR.exists():
    GEO_DIR = Path.home() / "git" / "geo"


MAX_TOOL_OUTPUT = 4000  # chars — keeps conversation lean


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Truncate text, noting how much was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} chars truncated]"


def _run_geo(args: list[str], timeout: int = 600) -> dict:
    """Run a geo tool via conda run -n geo python ... and return structured result."""
    cmd = ["conda", "run", "-n", "geo", "python"] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        output = {
            "stdout": _truncate(result.stdout),
            "stderr": _truncate(result.stderr, 2000),
            "returncode": result.returncode,
        }
        if result.returncode != 0:
            output["status"] = "error"
        else:
            output["status"] = "success"
        return output
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def execute_tool(name: str, input: dict) -> str:
    """Execute a tool call."""
    if name == "transform_dc":
        dc_path = str(Path(input["dc_path"]).expanduser())
        args = [str(GEO_DIR / "transform.py"), "dc", dc_path]
        if input.get("out_dir"):
            args += ["--out-dir", str(Path(input["out_dir"]).expanduser())]
        if input.get("job"):
            args += ["--job", input["job"]]
        if input.get("anchor"):
            args += ["--anchor"] + input["anchor"].split()
        return json.dumps(_run_geo(args))

    if name == "run_sight":
        survey_csv = str(Path(input["survey_csv"]).expanduser())
        images_dir = str(Path(input["images_dir"]).expanduser())
        args = [str(GEO_DIR / "TargetSighter" / "sight.py"), survey_csv, images_dir]
        if input.get("out_dir"):
            args += ["--out-dir", str(Path(input["out_dir"]).expanduser())]
        if input.get("n_control"):
            args += ["--n-control", str(input["n_control"])]
        if input.get("crs"):
            args += ["--crs", input["crs"]]
        # Auto-detect cameras.json if not explicitly provided
        cameras = input.get("cameras")
        if not cameras:
            survey_dir = Path(survey_csv).parent
            for candidate in [
                survey_dir / "cameras.json",
                survey_dir / "opensfm" / "cameras.json",
            ]:
                if candidate.exists():
                    cameras = str(candidate)
                    break
        if cameras:
            args += ["--cameras", str(Path(cameras).expanduser())]
        if input.get("nadir_weight") is not None:
            args += ["--nadir-weight", str(input["nadir_weight"])]
        # sight.py can be slow — give it 30 minutes
        result = _run_geo(args, timeout=1800)
        if cameras and "cameras" not in (input or {}):
            result["auto_cameras"] = cameras
        return json.dumps(result)

    if name == "transform_split":
        tagged_path = str(Path(input["tagged_path"]).expanduser())
        args = [str(GEO_DIR / "transform.py"), "split", tagged_path]
        if input.get("out_dir"):
            args += ["--out-dir", str(Path(input["out_dir"]).expanduser())]
        return json.dumps(_run_geo(args))

    if name == "run_rmse":
        job_dir = Path(input["job_dir"]).expanduser()

        # Auto-detect reconstruction
        recon = input.get("reconstruction")
        if not recon:
            for candidate in [
                job_dir / "opensfm" / "reconstruction.topocentric.json",
            ]:
                if candidate.exists():
                    recon = str(candidate)
                    break

        # Auto-detect gcp/chk
        gcp = input.get("gcp")
        if not gcp:
            c = job_dir / "gcp_list.txt"
            if c.exists():
                gcp = str(c)
        chk = input.get("chk")
        if not chk:
            c = job_dir / "chk_list.txt"
            if c.exists():
                chk = str(c)

        # Auto-detect orthophoto
        ortho = input.get("ortho")
        if not ortho:
            for candidate in [
                job_dir / "odm_orthophoto" / "odm_orthophoto.original.tif",
                job_dir / "odm_orthophoto" / "odm_orthophoto.original_cog.tif",
                job_dir / "odm_orthophoto" / "odm_orthophoto.tif",
            ]:
                if candidate.exists():
                    ortho = str(candidate)
                    break

        # Build args
        args = [str(GEO_DIR / "rmse.py")]

        has_recon = recon is not None
        if has_recon:
            args.append(recon)

        if gcp:
            args += ["--gcp", gcp]
        if chk:
            args += ["--chk", chk]
        if ortho:
            args += ["--ortho", ortho]

        # HTML report
        html = input.get("html")
        if not html:
            if input.get("emit_ortho_tags") and has_recon:
                html = str(job_dir / "rmse-recon.html")
            elif input.get("ortho_tags") or has_recon:
                html = str(job_dir / "rmse.html")
        if html:
            args += ["--html", html]

        if input.get("emit_ortho_tags"):
            args.append("--emit-ortho-tags")
        if input.get("ortho_tags"):
            args += ["--ortho-tags", str(Path(input["ortho_tags"]).expanduser())]

        # Report what was auto-detected
        detected = {}
        if recon and "reconstruction" not in input:
            detected["reconstruction"] = recon
        if gcp and "gcp" not in input:
            detected["gcp"] = gcp
        if chk and "chk" not in input:
            detected["chk"] = chk
        if ortho and "ortho" not in input:
            detected["ortho"] = ortho

        result = _run_geo(args)
        if detected:
            result["auto_detected"] = detected
        result["has_reconstruction"] = has_recon
        if html:
            result["html_report"] = html
        return json.dumps(result)

    if name == "get_job_state":
        job_dir = Path(input["job_dir"]).expanduser()
        state_file = job_dir / ".odium-state.json"
        if not state_file.exists():
            return json.dumps({
                "stage": "NEW",
                "job_dir": str(job_dir),
                "history": [],
            })
        try:
            state = json.loads(state_file.read_text())
            state["job_dir"] = str(job_dir)
            return json.dumps(state)
        except Exception as e:
            return json.dumps({"error": f"Could not read state: {e}"})

    if name == "update_job_state":
        job_dir = Path(input["job_dir"]).expanduser()
        state_file = job_dir / ".odium-state.json"
        # Load existing or create new
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                state = {"history": []}
        else:
            state = {"history": []}
        old_stage = state.get("stage", "NEW")
        new_stage = input["stage"]
        state["stage"] = new_stage
        state["job_dir"] = str(job_dir)
        state["job_name"] = job_dir.name
        # Append to history
        from datetime import datetime, timezone
        entry = {
            "from": old_stage,
            "to": new_stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if input.get("notes"):
            entry["notes"] = input["notes"]
        state.setdefault("history", []).append(entry)
        # Merge any extra metadata from notes (e.g. instance_id, cost)
        if input.get("metadata"):
            state.setdefault("metadata", {}).update(
                json.loads(input["metadata"]) if isinstance(input["metadata"], str)
                else input["metadata"]
            )
        try:
            state_file.write_text(json.dumps(state, indent=2) + "\n")
            return json.dumps({"status": "success", "stage": new_stage, "job_dir": str(job_dir)})
        except Exception as e:
            return json.dumps({"error": f"Could not write state: {e}"})

    if name == "save_session_summary":
        job_dir = Path(input["job_dir"]).expanduser()
        state_file = job_dir / ".odium-state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except Exception:
                state = {}
        else:
            state = {}
        from datetime import datetime, timezone
        state["last_session_summary"] = input["summary"]
        state["last_session_timestamp"] = datetime.now(timezone.utc).isoformat()
        try:
            state_file.write_text(json.dumps(state, indent=2) + "\n")
            return json.dumps({"status": "success"})
        except Exception as e:
            return json.dumps({"error": f"Could not write state: {e}"})

    if name == "open_in_browser":
        target = input["path_or_url"]
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)
            return json.dumps({"status": "success", "opened": target})
        else:
            path = Path(target).expanduser()
            if not path.exists():
                return json.dumps({"error": f"File not found: {path}"})
            webbrowser.open(f"file://{path.resolve()}")
            return json.dumps({"status": "success", "opened": str(path.resolve())})

    if name == "list_files":
        path = Path(input["path"]).expanduser()
        pattern = input.get("pattern")
        if not path.exists():
            return json.dumps({"error": f"Path not found: {path}"})
        if not path.is_dir():
            return json.dumps({"error": f"Not a directory: {path}"})
        entries = []
        for entry in sorted(path.iterdir()):
            if pattern and not fnmatch.fnmatch(entry.name, pattern):
                continue
            try:
                stat = entry.stat()
                size = stat.st_size
                if size > 1_000_000_000:
                    size_str = f"{size / 1_000_000_000:.1f} GB"
                elif size > 1_000_000:
                    size_str = f"{size / 1_000_000:.1f} MB"
                elif size > 1_000:
                    size_str = f"{size / 1_000:.1f} KB"
                else:
                    size_str = f"{size} B"
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": size_str,
                })
            except OSError:
                entries.append({"name": entry.name, "type": "unknown"})
        total = len(entries)
        shown = entries[:50]  # cap at 50 entries to save tokens
        return json.dumps({
            "path": str(path),
            "total_count": total,
            "shown": len(shown),
            "entries": shown,
            "truncated": total > 50,
        })

    if name == "read_file":
        path = Path(input["path"]).expanduser()
        max_lines = input.get("max_lines", 200)
        if not path.exists():
            return json.dumps({"error": f"File not found: {path}"})
        if not path.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        try:
            if path.suffix.lower() == ".pdf":
                import fitz  # pymupdf
                doc = fitz.open(str(path))
                pages = []
                for i, page in enumerate(doc):
                    text = page.get_text().strip()
                    if not text:
                        # Scanned/image PDF — OCR it
                        import pytesseract
                        from PIL import Image
                        import io
                        pix = page.get_pixmap(dpi=300)
                        img = Image.open(io.BytesIO(pix.tobytes("png")))
                        text = pytesseract.image_to_string(img)
                    pages.append(f"--- Page {i + 1} ---\n{text}")
                doc.close()
                return json.dumps({
                    "path": str(path),
                    "pages": len(pages),
                    "content": "\n".join(pages),
                })
            else:
                text = "\n".join(path.read_text(errors="replace").splitlines()[:max_lines])
                return json.dumps({
                    "path": str(path),
                    "content": _truncate(text),
                })
        except Exception as e:
            return json.dumps({"error": f"Could not read {path}: {e}"})

    if name == "ngs_lookup":
        import re
        import urllib.request
        results = {}

        pid = input.get("pid")
        spc_zone = input.get("spc_zone")

        # --- Direct PID lookup ---
        if pid:
            # Fetch datasheet
            ds_url = f"https://www.ngs.noaa.gov/cgi-bin/ds_mark.prl?PidBox={pid}"
            try:
                req = urllib.request.Request(ds_url, headers={"User-Agent": "odium/0.1"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    text = resp.read().decode("utf-8", errors="replace")

                # Extract monument name
                name_match = re.search(r"Designation\s*-\s*(.+)", text)
                results["pid"] = pid
                results["name"] = name_match.group(1).strip() if name_match else ""

                # Extract lat/lon
                lat_match = re.search(r"POSITION\s*-\s*([\d.]+)\(N\)", text)
                lon_match = re.search(r"([\d.]+)\(W\)", text)
                if lat_match and lon_match:
                    results["lat"] = float(lat_match.group(1))
                    results["lon"] = -float(lon_match.group(1))

                # Parse SPC if zone provided
                if spc_zone:
                    pattern = (r";SPC\s+" + re.escape(spc_zone) +
                               r"\s*-\s*([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+sFT")
                    m = re.search(pattern, text)
                    if m:
                        results["state_n_ft"] = float(m.group(1).replace(",", ""))
                        results["state_e_ft"] = float(m.group(2).replace(",", ""))
                        results["source"] = "NGS datasheet (exact)"
                    else:
                        results["spc_note"] = f"No SPC line for zone '{spc_zone}' in datasheet"

                # Fallback to NCAT if we have lat/lon but no SPC
                if "state_e_ft" not in results and results.get("lat"):
                    # Try NCAT
                    try:
                        ncat_url = (
                            f"https://geodesy.noaa.gov/api/ncat/llh?"
                            f"lat={results['lat']:.10f}&lon={results['lon']:.10f}&eht=0"
                            f"&inDatum=nad83%282011%29&outDatum=nad83%282011%29"
                            f"&units=usft"
                        )
                        req2 = urllib.request.Request(ncat_url, headers={"User-Agent": "odium/0.1"})
                        with urllib.request.urlopen(req2, timeout=10) as resp2:
                            ncat = json.loads(resp2.read())
                        results["state_e_ft"] = float(ncat.get("spcEasting_usft", "0").replace(",", ""))
                        results["state_n_ft"] = float(ncat.get("spcNorthing_usft", "0").replace(",", ""))
                        results["spc_zone_ncat"] = ncat.get("spcZone", "")
                        results["source"] = "NCAT lat/lon (~20 ft accuracy)"
                    except Exception as e:
                        results["ncat_error"] = str(e)

            except Exception as e:
                results["error"] = f"Failed to fetch datasheet for {pid}: {e}"

        # --- Radial search ---
        elif input.get("lat") and input.get("lon"):
            lat, lon = input["lat"], input["lon"]
            radius = min(input.get("radius_miles", 5), 10)
            api_url = (
                f"https://geodesy.noaa.gov/api/nde/stationlist?"
                f"lat={lat}&lon={lon}&radius={radius}&units=usft"
            )
            try:
                req = urllib.request.Request(api_url, headers={"User-Agent": "odium/0.1"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    stations = json.loads(resp.read())
                results["stations"] = [
                    {"pid": s.get("pid"), "name": s.get("name"),
                     "lat": s.get("latitude"), "lon": s.get("longitude"),
                     "dist_miles": s.get("distance")}
                    for s in stations[:20]  # cap at 20
                ]
                results["count"] = len(stations)
            except Exception as e:
                results["error"] = f"NGS radial search failed: {e}"
        else:
            results["error"] = "Provide either 'pid' or 'lat'+'lon'"

        return json.dumps(results)

    if name == "fetch_url":
        import requests
        url = input["url"]
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "odium/0.1"})
            return json.dumps({
                "url": url,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "content": _truncate(resp.text),
            })
        except Exception as e:
            return json.dumps({"error": f"Failed to fetch {url}: {e}"})

    if name == "list_jobs":
        return json.dumps({
            "jobs": [
                {"name": "aztec7", "stage": "RMSE_DONE", "local_size_gb": 12.4},
                {"name": "redrocks", "stage": "TAGGED", "local_size_gb": 8.1},
            ],
            "note": "(STUB — hardcoded example data)",
        })

    return json.dumps({"error": f"Unknown tool: {name}"})


def run_agent():
    """Interactive agent loop in the terminal."""
    messages = []
    print("odium pipeline agent (type 'quit' to exit)\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            # Try to save summary on interrupt too
            if messages:
                print("\n  [saving session summary...]")
                try:
                    messages.append({"role": "user", "content":
                        "Session interrupted. Call save_session_summary with "
                        "a brief summary of what was done and next steps."})
                    response = client.messages.create(
                        model=MODEL, max_tokens=512,
                        system=SYSTEM_PROMPT, tools=TOOLS,
                        messages=messages,
                    )
                    for block in response.content:
                        if block.type == "tool_use":
                            execute_tool(block.name, block.input)
                except Exception:
                    pass
            print("bye")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            # Ask the agent to save a session summary before exiting
            if messages:
                messages.append({"role": "user", "content":
                    "The session is ending. If you worked on a job this session, "
                    "please call save_session_summary now with a concise summary "
                    "of what was done and what the next step should be. "
                    "If no job was worked on, just say goodbye."})
                # Run one more agent turn to let it save
                try:
                    response = client.messages.create(
                        model=MODEL, max_tokens=1024,
                        system=SYSTEM_PROMPT, tools=TOOLS,
                        messages=messages,
                    )
                    # Execute any tool calls (should be save_session_summary)
                    for block in response.content:
                        if block.type == "tool_use":
                            print(f"\n  [{block.name}] saving session summary...")
                            execute_tool(block.name, block.input)
                        elif hasattr(block, "text"):
                            print(f"\nodium> {block.text}")
                except Exception:
                    pass  # don't block exit on errors
            print("bye")
            break

        messages.append({"role": "user", "content": user_input})

        # Agentic loop: keep going while Claude wants to use tools
        while True:
            # Retry with backoff on rate limits
            for attempt in range(5):
                try:
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=1024,
                        system=SYSTEM_PROMPT,
                        tools=TOOLS,
                        messages=messages,
                    )
                    break
                except anthropic.RateLimitError as e:
                    wait = min(2 ** attempt * 10, 120)  # 10s, 20s, 40s, 80s, 120s
                    print(f"\n  [rate limited — waiting {wait}s before retry]")
                    time.sleep(wait)
            else:
                print("\n  [rate limit exceeded after 5 retries — try again later]")
                break

            # Collect text blocks to print
            text_parts = []
            tool_calls = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            # Print any text the model produced
            if text_parts:
                print(f"\nodium> {''.join(text_parts)}")

            # If no tool use, we're done with this turn
            if response.stop_reason != "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                break

            # Execute tool calls
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for call in tool_calls:
                print(f"\n  [{call.name}] {json.dumps(call.input, indent=2)}")
                result = execute_tool(call.name, call.input)
                print(f"  -> {result[:120]}...")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})
            # Loop back — Claude may want to use more tools or produce final text

    print()


if __name__ == "__main__":
    run_agent()
