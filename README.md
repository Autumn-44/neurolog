# NeuroLog AI

> Last updated: November 2, 2026

Project repository: [shahrukh-210906/neurolog](https://github.com/shahrukh-210906/neurolog).

Live application logging with a React dashboard, Flask ingestion API, SQLite storage, and TF-IDF/DBSCAN analysis. Opens directly without login on the local machine.

## Start

Run `powershell -ExecutionPolicy Bypass -File .\start-preview.ps1` from this folder. Python 3.12 and Node.js are required. The launcher installs missing dependencies and starts three background services.

- Python task application: http://127.0.0.1:8000
- NeuroLog dashboard: http://127.0.0.1:5173/dashboard

Create a task and complete it. The application sends actual task events, request durations/status codes, and measured service heartbeats to NeuroLog. Open Log Explorer to see the `task-service` stream. The dashboard polls every two seconds; Explorer every three seconds. Run clustering to refresh ML analysis.

The sender persists events in a SQLite outbox and retries failed delivery. Optional random generation is available in the Python app, with a rate, finite limit, and Stop control. Generated messages carry [GENERATED] and are treated separately by analysis. Current received logs use `data/neurolog-live.db`; previous records remain preserved in the older database.

## Documentation

Read `docs/USER_GUIDE.md` or `output/pdf/NeuroLog_User_Guide.pdf` for architecture, integration, settings, and troubleshooting.

## GitHub Actions

Open **GitHub Actions** in the sidebar (or `/github`). Connect `owner/repository`, choose **Find failed runs**, then **Import & analyze**. Imported logs appear as a `github:owner/repository` application with build, commit, and available pull-request links. Failure summaries use local rules, cite imported record IDs, and suggest investigation steps; they do not establish a root cause.

Public repository browsing works without a token. Private repositories and restricted log downloads require a fine-grained GitHub token with **Actions: Read** on the selected repository (and repository metadata access). Add **Contents: Read** to verify later-commit ancestry in private repositories. Enter it in the password field; NeuroLog keeps it only in page memory and uses it for the current requests. It is not stored in the database, browser storage, or environment. Leaving/reloading the page clears it. Do not use private logs in a shared public demo.

Imports are manual and deduplicated by repository, run ID, and attempt. The latest 20 failed runs are listed. Each archive is limited to 8 MB compressed, 12 MB expanded, 500 text files, and 100,000 scanned lines. Up to 1,500 nonempty lines are retained, prioritizing errors. Truncated imports are labeled. GitHub's combined job files are skipped when individual step files are present, avoiding duplicate evidence. Common token/credential patterns are redacted, but this is not a complete secret scanner.

Logs follow the existing retention policy; saved summaries remain available until **Delete logs** is used, which also clears GitHub import history and permits reimporting. Repository connections remain saved. There is no automatic monitoring, repository modification, or issue creation in this first version.

The backend uses GitHub's [workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs) and downloads the selected attempt's archive. Expired logs, unavailable repositories, access errors, and rate limits produce actionable messages. Test coverage uses mocked GitHub responses and real ZIP archives, without requiring credentials.

Every failed run and saved finding includes a later-commit tag. **Likely resolved** means the latest comparable workflow passed on a verified descendant commit. **Workflow still failing** means that later workflow failed; it may have different errors. **Not verified** covers missing evidence, no comparable later commit, unfinished runs, or unavailable access. These are workflow-level signals, not proof that an individual error was fixed.

Checks inspect the latest 100 repository runs, matching workflow, branch, event, source repository, and pull-request numbers. Same-commit reruns do not count as later commits. Select a run’s outcome for the reason, check time, and later run/commit links. Saved findings keep expandable tags alongside their evidence. Use **Check later commits** on a saved summary to refresh its evidence. Checks run on request; cached tags do not update continuously.

## Interface

The workspace uses a compact sidebar, neutral light/dark themes, responsive panels, and restrained transitions. Choose an application to open its overview, log explorer, investigations, and assistant. The selection survives a reload in the same tab. Settings includes light, dark, and system appearance. Motion respects the operating system's reduced-motion preference.

See [the design review](docs/UI_REDESIGN.md) for the Stageshow comparisons and validation notes.

## Validation

Run `.venv/Scripts/python.exe -m pytest -q`. In `logintel_ui`, run `npm run lint` and `npm run build`.

Keep the login-free workspace bound to localhost. Optional Groq configuration enables conversational analysis; otherwise the assistant summarizes actual received records. Clustering flags unusual messages for review and does not prove failure or execute remediation.

Original project: https://github.com/Sai-Nitin123/Neurolog-AI. Original MIT license and contributor attribution retained.

## Incident analysis

The AI Assistant groups warnings/errors by category and source, cites record IDs, and proposes investigation steps. Enter `source:task-service` to narrow local analysis. The interface distinguishes local rule-based analysis from Groq language-model responses. Set GROQ_API_KEY in the backend environment for conversational analysis. Provider failure returns a labeled local fallback.

## Investigation workbench and pipeline lab

Open `/incidents` for windowed risk scoring, entity/trace history, real TF-IDF neighbors, alert rules, reversible suppression, and filtered CSV export. Open `/pipeline` for the isolated reference simulator. Read [the complete feature comparison and guide](docs/REFERENCE_FEATURE_COMPARISON.md) for all 37 additions and the distinction between live analysis and illustrative lab controls.
