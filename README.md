# HCP Terraform Organization Backup/Restore

This repository provides a production-oriented CLI script to back up and restore HCP Terraform organizations.

The goal is disaster recovery readiness:

- Recover **configuration + state** quickly after accidental deletion.
- Preserve an audit snapshot (including optional run history export).
- Restore operational continuity, even though historical runs are not re-importable as native run records.

## Disclaimer

This repository is a community best-effort automation example for HCP Terraform backup/restore workflows.

- It is **not official HashiCorp documentation**.
- It is **not a supported HashiCorp product** and has **no SLA/support guarantee**.
- It is provided **as-is**, without warranties.
- You are responsible for validation, testing, and safe operation in your environment.

Always test in non-production environments before using in production.

## What This Tool Does

### Backup

- Exports organization metadata and topology:
  - organization, projects, workspaces
  - workspace variables
  - variable sets and variable set variables
  - tags (enhanced + legacy flat tags)
  - remote state consumers
  - team access relationships and teams (best-effort)
- Downloads the latest N finalized state versions per workspace (`.tfstate`).
- Optionally exports workspace run history (audit JSON).
- Optionally uploads each backup to S3 or GCS.

### Restore

- Recreates organization/project/workspace structure (best-effort, idempotent-friendly).
- Restores variables, variable sets, tags, and remote state consumers.
- Restores state versions using lock -> upload -> finalize -> unlock flow.
- Attempts to restore teams and workspace-level team access.

## What This Tool Does Not Do

- It does **not** re-import historical runs as native HCP Terraform run history.
- It can export runs for audit (`runs/*.json`), but those are not restorable as first-class run objects.

## Requirements

- Python 3.9+
- `requests` dependency (`pip install -r requirements.txt`)
- HCP Terraform API token with sufficient permissions
  - For state-version operations, use a **user token** or **team token** (not an organization token).

Optional (only if using remote upload):

- AWS CLI configured for `s3://...`
- `gsutil` configured for `gs://...`

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set token:

```bash
export HCP_TF_TOKEN="..."
```

## Authentication and Token Strategy

This tool uses a single bearer token via `HCP_TF_TOKEN` (or `--token`).

### Token types in HCP Terraform (practical view)

- **User token**
  - Created in user settings.
  - Broadest compatibility with API endpoints used by backup/restore.
  - Good for manual operations and controlled automation.
- **Team token**
  - Bound to a specific team and its permissions.
  - Recommended for CI/CD and pipeline automation when possible (least-privilege model).
  - Must include required permissions across all target resources.
- **Organization token**
  - Useful for some org automation tasks, but **not sufficient** for all state-version flows used here.
  - Not recommended as the primary token for this backup/restore implementation.

### Recommended for CI/CD

For non-interactive automation, prefer a **dedicated team token** with only the permissions required for:

- reading workspaces/projects/vars/varsets
- reading and creating state versions
- lock/unlock operations
- optional team/access management if you use full restore scope

If your org permission model is complex, start with a user token to validate, then migrate to a scoped team token.

### Where to create tokens

- **User token**: HCP Terraform user profile/settings -> tokens.
- **Team token**: Team settings -> API/team token.

Store tokens in your CI/CD secret manager (never in git):

- GitHub Actions secrets, GitLab CI variables, Azure DevOps secret variables, etc.

## Usage

### Basic backup (local)

```bash
python3 hcp_tf_backup_restore.py backup --org "<ORG_NAME>"
```

Output path format:

- `./backups/<ORG_NAME>/<YYYYmmdd-HHMMSS>/`

By default, the folder timestamp is generated in `UTC`.
You can override timezone with:

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --backup-timezone "Europe/Madrid"
```

### Backup with custom number of state versions

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --state-versions 10
```

### Backup with run history export

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --state-versions 5 \
  --export-runs-history
```

### Incremental run history export (since previous backup)

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --state-versions 5 \
  --export-runs-history \
  --runs-since-last-backup
```

Notes:

- Default behavior remains full run-history export.
- `--runs-since-last-backup` only affects run-history export, not state/config backup.

### Backup + S3 upload

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --state-versions 5 \
  --upload "s3://my-bucket/hcp-tf-backups"
```

### Backup + GCS upload

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --state-versions 5 \
  --upload "gs://my-bucket/hcp-tf-backups"
```

### Restore from backup

```bash
python3 hcp_tf_backup_restore.py restore \
  --backup-dir "./backups/<ORG_NAME>/<YYYYmmdd-HHMMSS>"
```

Restore to a different target org:

```bash
python3 hcp_tf_backup_restore.py restore \
  --backup-dir "./backups/<ORG_NAME>/<YYYYmmdd-HHMMSS>" \
  --org-name "<TARGET_ORG>" \
  --org-email "admin@example.com"
```

Important restore notes:

- `--org-email` is only needed when the target org must be created.
- If target org already exists, `--org-email` is optional.
- If you want to reuse the exact name of a deleted org, open a HashiCorp support ticket to re-enable that org name first (may take time).
- Safety default: restore aborts if the target org already contains resources (workspaces/varsets/non-default projects/non-owners teams).
- To intentionally restore into a non-empty org, pass `--allow-non-empty-target`.

## Backup Artifacts

Each backup folder contains:

- `manifest.json` (backup metadata and parameters)
- `org.json`, `projects.json`, `workspaces.json`, `varsets.json`, `teams.json`
- `workspaces/<workspace>/...` scoped workspace snapshots
- `states/<workspace>/state-versions.json`
- `states/<workspace>/sv-<id>.tfstate`
- `runs/<workspace>-runs.json` (if `--export-runs-history` is enabled)

After restore:

- `restore-report.json` is generated with warnings and pending items.

## Known Limitations

- Sensitive variable values may be unreadable via API and therefore not restorable automatically.
- Some integration-specific settings (VCS/OAuth/agent pools) can require manual reconfiguration.
- User membership recovery can require invitation acceptance workflow.
- Existing resources are handled in best-effort mode and may be skipped to avoid destructive behavior.

## Recommended Operations

- Schedule daily backups.
- Store backups in local + encrypted remote storage (S3/GCS).
- Periodically test restore in a non-production org.
- Keep a DR runbook: see `DR_CHECKLIST.md`.

## Scaling to Multiple Organizations

This repository is intentionally focused on a **single organization per execution**.

For large-scale environments, treat this project as a base building block and add an orchestration layer that:

- iterates through a managed list of organizations,
- handles per-org credentials and policy boundaries,
- applies rate limits/retries/observability,
- stores centralized execution logs and compliance evidence.

In other words: this tool is the engine; enterprise-wide multi-org automation is typically implemented as an additional platform layer by each organization.

## Security

- Treat backup artifacts as sensitive data.
- Do not commit backups to public repositories.
- Enforce encryption at rest and strict IAM access policies for remote storage.

