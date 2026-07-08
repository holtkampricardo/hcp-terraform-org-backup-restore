# Client Demo Guide - HCP Terraform Org Backup & Restore

This guide walks you through a **safe, repeatable client demo** of backup and restore.

Recommended demo pattern:

- **Source org**: your sandbox org (set as `SOURCE_ORG` in `.env`)
- **Target org**: empty DR org (set as `TARGET_ORG` in `.env`)
- **Never restore into production** unless explicitly agreed with the client.

Estimated duration: **25-35 minutes** (including Q&A checkpoints).

---

## 1) Demo objectives (what the client should understand)

By the end of the demo, the client should see that the tool can:

1. Export org topology + configuration + Terraform state.
2. Restore into a new empty org without manual reconstruction.
3. Produce auditable output (`manifest.json`, `restore-report.json`).
4. Highlight practical limitations (secrets, VCS/OAuth, run history audit-only).

Key message to repeat: **no backup, no restore**.

---

## 2) Prerequisites

### 2.1 Environment

- Python 3.9+
- Git clone of this repository
- Network access to `https://app.terraform.io/api/v2`
- HCP Terraform user token or team token with enough permissions

### 2.2 HCP Terraform setup

Demo orgs (configure locally in `.env`, not committed to git):

- `SOURCE_ORG` -> your sandbox/source org
- `TARGET_ORG` -> empty DR org for restore demo
- `ADMIN_EMAIL` -> email used if target org must be created

In the source org, ideally include:

- 1-2 projects
- 2-3 workspaces (at least one with state)
- 1 variable set
- 1-2 teams with workspace access (optional but good for demo)

### 2.3 Token permissions

The token should allow at minimum:

- read org/projects/workspaces/vars/varsets/teams
- read and write state versions
- lock/unlock workspaces
- create org/projects/workspaces/vars/varsets (for restore demo)

Use a **user token** or **team token** (not organization token) for state operations.

---

## 3) Local setup (before the meeting)

Run these commands once before the demo.

### 3.1 Clone and install dependencies

```bash
git clone <REPO_URL>
cd hcp-terraform-org-backup-restore
```

**Why:** gives you the CLI script and documentation used in the demo.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Why:** isolates Python dependencies (`requests`) and avoids polluting system Python.

### 3.2 Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` with your local values:

```bash
HCP_TF_TOKEN=...
SOURCE_ORG=...
TARGET_ORG=...
ADMIN_EMAIL=...
```

**Why:**

- `.env` keeps secrets out of shell history and git.
- The script auto-loads `.env` on startup (`HCP_TF_TOKEN`).
- Shell variables (`SOURCE_ORG`, `TARGET_ORG`, `ADMIN_EMAIL`) are loaded when you `source .env`.

Load variables into your shell for demo commands:

```bash
set -a
source .env
set +a
```

Optional sanity check:

```bash
python3 hcp_tf_backup_restore.py --help
```

**Why:** confirms the CLI starts correctly and reads `HCP_TF_TOKEN` from `.env`.

---

## 4) Demo flow (live script)

Demo values come from `.env` (`SOURCE_ORG`, `TARGET_ORG`, `ADMIN_EMAIL`).

Before running commands:

```bash
set -a
source .env
set +a
```

---

### Step 1 - Explain source environment (UI, 2-3 min)

In HCP Terraform UI (source org), show:

- Projects and workspaces list
- One workspace with recent state
- Variables / variable sets
- (Optional) team access on one workspace

**Why:** establishes the "before" state that backup will capture.

---

### Step 2 - Run backup (terminal, 5-8 min)

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "$SOURCE_ORG" \
  --state-versions 5 \
  --export-runs-history
```

#### What this command does

- `backup`: export mode (read from HCP Terraform, write local files).
- `--org "$SOURCE_ORG"`: source organization to back up.
- `--state-versions 5`: downloads latest 5 finalized state versions per workspace.
- `--export-runs-history`: exports run history JSON for audit (`runs/*.json`).

#### Why these flags in a demo

- `--state-versions 5` is enough to prove state recovery without excessive runtime.
- `--export-runs-history` demonstrates audit capability, while you explain it is **not restorable** as native runs.

#### Expected output folder

```text
./backups/<SOURCE_ORG>/<YYYYmmdd-HHMMSS>/
```

Show the client key files:

```bash
ls -la "backups/$SOURCE_ORG/$(ls -1t backups/$SOURCE_ORG | head -n 1)"
```

Recommended files to open live:

```bash
export BACKUP_DIR="$(ls -1dt backups/$SOURCE_ORG/* | head -n 1)"
cat "$BACKUP_DIR/manifest.json"
ls "$BACKUP_DIR/states"
ls "$BACKUP_DIR/workspaces"
```
```

**Talking points while showing files:**

- `manifest.json` -> backup metadata and counters.
- `workspaces/` -> per-workspace config snapshots.
- `states/` -> downloaded `.tfstate` artifacts (critical for DR).
- `runs/` -> audit-only history export.

Set a shell variable for next steps:

```bash
echo "$BACKUP_DIR"
```

**Why:** avoids typing the timestamp folder manually during restore.

---

### Step 3 - Optional: remote upload demo (2-4 min)

Only include this if client uses S3/GCS and credentials are already configured.

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "$SOURCE_ORG" \
  --state-versions 5 \
  --upload "s3://<BUCKET>/hcp-tf-backups/$SOURCE_ORG"
```

#### What this does

- Creates local backup first.
- Uploads backup folder to remote prefix.
- Deletes local backup folder only after successful upload.

#### Why it matters

- Shows offsite retention pattern for production DR.
- Reinforces "backup must exist before incident".

If upload fails (permissions, credentials), explain that local backup is preserved for retry.

---

### Step 4 - Explain restore target (UI, 1-2 min)

Open target org (`$TARGET_ORG`) and confirm it is empty.

**Why:** restore has a safety check and aborts if target org already has resources (unless override flag is used).

---

### Step 5 - Run restore into DR org (terminal, 8-12 min)

```bash
python3 hcp_tf_backup_restore.py restore \
  --backup-dir "$BACKUP_DIR" \
  --org-name "$TARGET_ORG" \
  --org-email "$ADMIN_EMAIL"
```

#### What this command does

- `restore`: rebuild mode (read backup files, write to HCP Terraform).
- `--backup-dir "$BACKUP_DIR"`: exact backup snapshot to use.
- `--org-name "$TARGET_ORG"`: destination org (different from source in demo).
- `--org-email "$ADMIN_EMAIL"`: used only if destination org does not exist.

#### Why restore to a different org in demos

- Safer: no risk to source environment.
- Clear proof of rebuild capability.
- Matches real DR pattern (recover into DR org).

#### What happens internally (explain to client)

1. Creates org/projects/workspaces if missing.
2. Restores vars, varsets, tags, remote-state consumers.
3. Restores state versions via `lock -> upload -> finalize -> unlock`.
4. Attempts teams and workspace access best-effort.
5. Writes `restore-report.json` with warnings/pending actions.

---

### Step 6 - Review restore report (terminal + UI, 3-5 min)

```bash
cat "$BACKUP_DIR/restore-report.json"
```

**Why:** this is your auditable recovery evidence and post-restore checklist.

Then in HCP Terraform UI (target org), validate:

- Projects/workspaces recreated
- Variables and variable sets present
- State visible in workspace **States** tab
- Team permissions (if included in source)

Optional quick state sanity command (if workspace name known):

```bash
# UI validation is usually clearer for clients
# but you can mention that state versions were restored from sv-*.tfstate files
ls "$BACKUP_DIR/states/<WORKSPACE_SLUG>"
```

---

### Step 7 - Close with limitations (2-3 min)

Explicitly call out:

- Historical runs are audit-only (`runs/*.json`), not native run restore.
- Sensitive variables may require manual re-entry.
- VCS/OAuth/agent pools may need manual reconfiguration.
- Scope is workspace-centric (policies/stacks/registry not included today).

Invite client to run a pilot in their own sandbox and review `restore-report.json` together.

---

## 5) Recommended demo commands (copy/paste block)

```bash
# 0) Setup
cd hcp-terraform-org-backup-restore
source .venv/bin/activate
set -a && source .env && set +a

# 1) Backup source org
python3 hcp_tf_backup_restore.py backup \
  --org "$SOURCE_ORG" \
  --state-versions 5 \
  --export-runs-history

# 2) Pick latest backup
export BACKUP_DIR="$(ls -1dt backups/$SOURCE_ORG/* | head -n 1)"
echo "Using backup: $BACKUP_DIR"

# 3) Restore into empty DR org
python3 hcp_tf_backup_restore.py restore \
  --backup-dir "$BACKUP_DIR" \
  --org-name "$TARGET_ORG" \
  --org-email "$ADMIN_EMAIL"

# 4) Review report
cat "$BACKUP_DIR/restore-report.json"
```

---

## 6) Troubleshooting during demo

### "Safety check failed: target organization is not empty"

**Cause:** target org already has workspaces/varsets/projects/teams.

**Demo fix:** use a fresh empty org, or (not recommended in client demo) add `--allow-non-empty-target`.

### "Missing token"

**Cause:** `HCP_TF_TOKEN` is empty or missing in `.env`.

**Fix:** edit `.env` and set `HCP_TF_TOKEN`, then:

```bash
set -a && source .env && set +a
```

### State restore warnings / not finalized in time

**Cause:** API latency or large state files.

**Fix:** rerun restore is usually not needed for already-created entities; inspect `restore-report.json` and validate state in UI. For repeated tests, increase polling:

```bash
python3 hcp_tf_backup_restore.py restore \
  --backup-dir "$BACKUP_DIR" \
  --org-name "$TARGET_ORG" \
  --state-poll-attempts 40 \
  --state-poll-interval-s 5
```

### Sensitive variable warnings

**Expected behavior:** API may not return sensitive values.

**Client message:** secrets require secure re-entry or secret-manager integration post-restore.

---

## 7) What not to do in a client demo

- Do not restore into production org.
- Do not use `--allow-non-empty-target` unless explicitly planned.
- Do not commit backup folders to git (they contain sensitive data).
- Do not promise full-platform clone (policies/stacks/registry are out of current scope).

---

## 8) Suggested post-demo follow-up for client

1. Run one backup in their sandbox.
2. Run one restore into `*-dr` org.
3. Validate 2-3 critical workspaces with test plans.
4. Decide retention model:
   - local only, or
   - local + S3/GCS upload.
5. Agree operational cadence (for example daily backup via CI).

---

## 9) Related docs

- `README.md` -> full usage and coverage matrix
- `DR_CHECKLIST.md` -> operational runbook for incidents
- `PRESENTATION.md` -> slide narrative for executive/technical audience
