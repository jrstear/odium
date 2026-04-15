# odium

A conversational pipeline agent for drone survey processing. Walks a surveyor through the full workflow — from receiving customer data to delivering orthophotos — using the [geo](https://github.com/jrstear/geo) toolchain under the hood.

The name is a play on [ODM](https://github.com/OpenDroneMap/ODM) (OpenDroneMap), the open-source photogrammetry engine that powers the processing pipeline.

## What it does

You talk to odium in a terminal. It runs the pipeline tools, tracks job state, and guides you through each step:

```
you> I have a new job called mesa1, customer sent a .dc file

odium> Let me take a look at what's in the directory...
  [list_files] {"path": "./"}
  [read_file] {"path": "./F100340 MESA.dc"}

Found 38 survey monuments in the .dc file. Converting from design grid
to state plane (EPSG:6529) via NGS auto-lookup...
  [transform_dc] {"dc_path": "./F100340 MESA.dc"}

Done — 38 targets extracted. Outputs:
  mesa1_6529.csv (for Emlid localization)
  mesa1_design.csv (design-grid coords)
  transform.yaml (CRS + shift params)

Ready for field survey when you are.
```

## Pipeline stages

odium tracks these stages per job (order is typical, not rigid):

| Stage | What happens |
|-------|-------------|
| GATHER_INFO | Collect customer inputs (.dc, CSV, KMZ), validate completeness |
| PLAN_FLIGHT | Flight planning (DJI tools; future: GCP placement suggestions) |
| SURVEY | Field survey with Emlid (odium can help troubleshoot NTRIP, etc.) |
| DC_PARSED | `transform.py dc` — parse .dc to survey CSVs + transform.yaml |
| SURVEY_LOADED | Emlid field-survey CSV loaded |
| IMAGES_LOADED | Drone images directory identified |
| SIGHT_DONE | `sight.py` — match targets to images |
| TAGGED | GCPs/CHKs tagged in GCPEditorPro |
| SPLIT_DONE | `transform.py split` — gcp_list.txt + chk_list.txt |
| ODM_RUNNING | ODM processing on EC2 |
| ODM_COMPLETE | Results downloaded from S3 |
| RMSE_RECON | `rmse.py` step 6a — reconstruction accuracy + ortho crop emit |
| ORTHO_TAGGED | Ortho crops tagged in GCPEditorPro |
| RMSE_ORTHO | `rmse.py` step 6b — orthophoto accuracy |
| QGIS_ODM | QGIS review in ODM coordinates |
| QGIS_DESIGN | QGIS review in design-grid coordinates |
| PACKAGED | `packager.py` — reproject to design grid, COG output |
| DELIVERED | Artifacts in Google Drive, customer notified |

## Tools (15)

| Tool | Purpose |
|------|---------|
| `transform_dc` | Parse Trimble .dc file to survey CSVs |
| `run_sight` | Match survey targets to drone images |
| `transform_split` | Split tagged file into GCP + CHK lists |
| `run_rmse` | Accuracy assessment (reconstruction and/or orthophoto) |
| `run_package` | Reproject orthophoto to design grid, produce COG deliverable |
| `list_files` | List directory contents |
| `read_file` | Read text files and PDFs (with OCR for scanned documents) |
| `fetch_url` | HTTP fetch (status endpoints, downloads) |
| `ngs_lookup` | Look up NGS monument state-plane coordinates |
| `open_in_browser` | Open HTML reports or GCPEditorPro in browser |
| `file_op` | Move, copy, delete, mkdir |
| `get_job_state` | Read job state from `.odium-state.json` |
| `update_job_state` | Update job stage + metadata |
| `save_session_summary` | Persist session context for cross-session continuity |
| `list_jobs` | List all known jobs (stub) |

## Setup

```bash
git clone https://github.com/jrstear/odium.git
cd odium
./setup.sh          # creates conda env, installs deps + tesseract
```

Create a `.env` file with your Anthropic API key:
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
conda run -n odium python agent.py
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ODIUM_MODEL` | `claude-haiku-4-5` | Model to use (e.g. `claude-sonnet-4-6` for higher quality) |
| `ODIUM_DISPLAY` | `500` | Terminal display truncation in chars (set high to see full output) |

### Dependencies

- **[geo](https://github.com/jrstear/geo)** — the underlying Python pipeline tools (`~/git/geo`)
- **conda env `geo`** — cv2, numpy, pyproj, GDAL (for the pipeline tools)
- **conda env `odium`** — anthropic SDK, pymupdf, pytesseract (for the agent)
- **tesseract** — OCR for scanned PDFs (installed by setup.sh)
- **Anthropic API key** — ~$0.20/session with Haiku

## How it works

odium is a single Python file (`agent.py`) built on the Anthropic Messages API. The architecture is simple:

```
User input
    |
    v
client.messages.create(system=SYSTEM_PROMPT, tools=TOOLS, messages=history)
    |
    v
Claude responds with text and/or tool_use requests
    |
    +-- tool_use? --> execute tool, append result, loop back
    |
    +-- end_turn? --> print response, wait for next input
```

The system prompt encodes the pipeline workflow, file naming conventions, confirmation policy, error handling, and domain knowledge. The tools are thin wrappers that call `geo` pipeline commands via `conda run -n geo python ...`.

Job state persists in `.odium-state.json` in each job directory. Session summaries are saved on exit and loaded on resume for cross-session continuity. State is treated as a hint — the agent always verifies against actual files on disk before acting.

## Design doc

Full design notes, architecture decisions, and future scope: [docs/plans/odium.md](docs/plans/odium.md)

## Status

Early prototype. Tested on real survey jobs. EC2/ODM launch, S3 sync, and delivery (Google Drive) are not yet wired up. See the [design doc](docs/plans/odium.md) for the full roadmap.
