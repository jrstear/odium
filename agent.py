"""odium — minimal agent loop for the geo drone survey pipeline."""

import anthropic
import fnmatch
import json
import os
import subprocess
import sys
import time
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
                   for step 6b.
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

# Tools
You have tools for each pipeline stage. Some are stubs during development
— that's fine, work with what you get back. When a tool returns a stub
result, acknowledge it naturally and continue the flow.

CRS, EPSG codes, file paths, and other job-specific values come from the
data and from transform.yaml — never assume a particular EPSG or path.

# File naming conventions (strict — do not deviate without explicit user request)

## Customer inputs
- Trimble data collector: `{customer}_{job}.dc` or `{job}.dc`
- Emlid field survey: `{job}_emlid_6529.csv` (or raw Emlid export CSV)
- Control sheet: `CONTROL SHEET.pdf` (often scanned, needs OCR)
- Drone images: `images/*.JPG`

## transform.py dc outputs
- `{job}_{epsg}.csv`     — survey coords in field CRS (e.g. {job}_6529.csv)
- `{job}_design.csv`     — design-grid coords (customer's coordinate system)
- `transform.yaml`       — CRS + shift params; auto-loaded by downstream tools

## sight.py outputs
- `{job}.txt`            — tagging file for GCPEditorPro (all targets, untagged)
- `marks_design.csv`     — Pix4D parallel workflow (design-grid coords)

## GCPEditorPro (tagging)
- Input:  `{job}.txt`
- Output: `{job}_tagged.txt` (Download button adds _tagged suffix; never overwrites input)
- IMPORTANT: when user says "I tagged" or "done tagging", always look for
  `{job}_tagged.txt`, NEVER use the untagged `{job}.txt` for split.
  If the _tagged file doesn't exist, ask — don't silently use the wrong file.

## transform.py split outputs
- `gcp_list.txt`              — GCP-tagged observations only (for ODM)
- `chk_list.txt`              — CHK-tagged observations only (for rmse.py)
- `{job}_targets.csv`         — one row per target, ODM CRS (EPSG:32613)
- `{job}_targets_design.csv`  — one row per target, design-grid coords

## S3 / EC2 layout
- `s3://{bucket}/{project}/images/`        — drone images
- `s3://{bucket}/{project}/gcp_list.txt`   — control file for ODM
- `{project}` is typically `{client}/{job}`

## ODM outputs (synced from S3)
- `opensfm/reconstruction.topocentric.json` — bundle adjustment result
- `odm_orthophoto/odm_orthophoto.original.tif` — orthophoto
- `odm_report/`                             — ODM processing report
- `cameras.json`                            — calibrated camera models

## rmse.py step 6a (reconstruction accuracy) outputs
- `rmse-recon.html`                                    — accuracy report
- `odm_orthophoto/odm_orthophoto.original.txt`         — ortho tagging file
- `odm_orthophoto/odm_orthophoto.original-crops/`      — one JPEG per target

## GCPEditorPro (ortho tagging, step 6b)
- Input:  `odm_orthophoto/odm_orthophoto.original.txt` + crops folder
- Output: `odm_orthophoto/odm_orthophoto.original_tagged.txt`

## rmse.py step 6b (orthophoto accuracy) outputs
- `rmse.html`  — full report with both reconstruction + orthophoto accuracy

## packager outputs
- Deliverables in design-grid CRS (reprojected + shifted via transform.yaml)

## Sanity checks
- 0 tagged observations after split → almost certainly used wrong file (untagged)
- 0 GCP or 0 CHK after split → user may not have assigned roles in GCPEditorPro
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
        if input.get("cameras"):
            args += ["--cameras", str(Path(input["cameras"]).expanduser())]
        if input.get("nadir_weight") is not None:
            args += ["--nadir-weight", str(input["nadir_weight"])]
        # sight.py can be slow — give it 30 minutes
        return json.dumps(_run_geo(args, timeout=1800))

    if name == "transform_split":
        tagged_path = str(Path(input["tagged_path"]).expanduser())
        args = [str(GEO_DIR / "transform.py"), "split", tagged_path]
        if input.get("out_dir"):
            args += ["--out-dir", str(Path(input["out_dir"]).expanduser())]
        return json.dumps(_run_geo(args))

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
            print("\nbye")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
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
