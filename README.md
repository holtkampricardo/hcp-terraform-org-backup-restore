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

## Coverage Matrix

This tool targets **workspace-centric disaster recovery**: rebuild org structure, restore configuration, and recover state. The matrix below separates what is supported today from known gaps and from items that are intentionally out of scope.

Legend:

| Symbol | Meaning |
| --- | --- |
| ✅ | Supported |
| ⚠️ | Partially supported |
| 🔜 | Feasible via public API, not implemented in this tool yet |
| ⛔ | Out of scope for this tool |

### Supported today

| Area | Backup | Restore | Notes |
| --- | --- | --- | --- |
| Projects | ✅ | ✅ | Name + description |
| Variable sets + variables | ✅ | ✅ | Global, project, and workspace scope |
| Remote state consumers | ✅ | ✅ | Remapped by workspace name |
| Workspace state versions | ✅ | ✅ | Latest N finalized `.tfstate` files per workspace |

### Partially supported

| Area | Backup | Restore | Gap / follow-up |
| --- | --- | --- | --- |
| Organization metadata | ✅ | ⚠️ | `org.json` is exported; restore mainly uses org name/email |
| Workspaces | ✅ | ⚠️ | Recreated with a subset of settings (VCS triggers, execution mode, etc.) |
| Workspace variables | ✅ | ⚠️ | Sensitive values may be unreadable via API |
| Tags | ✅ | ⚠️ | Enhanced tags usually restore; flat tags may fail across orgs |
| Teams + workspace access | ✅ | ⚠️ | Best-effort recreation of teams and permissions |
| Workspace VCS settings | ✅ | ⚠️ | Repo metadata is saved; OAuth/GitHub App must be reconnected manually |
| Agent-based workspaces | ✅ | ⚠️ | Falls back to `remote` if agent pool cannot be remapped |
| Run history (optional) | ⚠️ | ⛔ | `--export-runs-history` is audit-only (`runs/*.json`) |

### Not implemented yet (API-feasible)

These gaps can be automated with the public HCP Terraform API, but are not implemented in this repository yet:

| Area | API automation | Main caveat |
| --- | --- | --- |
| Policy sets and policies (Sentinel/OPA) | Yes | VCS-linked sets need OAuth reconnect; API-uploaded tarball sets are hard to re-export |
| Terraform Stacks | Yes (complex) | Different state lifecycle; larger implementation effort |
| Run Tasks | Yes | Task metadata restores; HMAC keys are write-only and may need re-entry |
| Agent pool configuration | Yes (partial) | Pool metadata/scope can be recreated; agents and agent tokens must be redeployed |
| Private Module/Provider Registry | Yes | Non-VCS artifacts can be mirrored; VCS-linked modules depend on repo access |
| Organization memberships | Yes (partial) | Users can be re-invited via API; invite acceptance is still required |
| Team project access | Yes | |
| Workspace notifications | Yes (partial) | URLs/triggers can be restored; notification auth tokens are write-only |

### Out of scope (by design)

These cannot be fully automated today, are secret-by-nature, or are not DR configuration:

- **API tokens** (org/team/user): secrets; must be rotated/recreated.
- **OAuth tokens / GitHub App credentials**: cannot be exported; reconnect integrations after restore.
- **SSH private keys**: HCP Terraform API only returns metadata; private key text is write-only.
- **SSO / SAML setup**: configured primarily via UI and IdP integration, not a full API backup/restore flow.
- **Notification auth tokens**: write-only; webhook URLs can be automated, secrets cannot.
- **Terraform Actions**: defined in Terraform code, not standalone org objects. They come back with workspace config + VCS + state.
- **Runs, plans, applies, policy checks, action invocations**: operational history, not configuration.
- **Workspace locks / live run queues**: transient runtime state.
- **Billing, entitlements, and subscription tier**: managed by the HCP Terraform platform.
- **Native 30-day recoverable items UI**: use HashiCorp platform recovery when available; this tool is for external backup/restore.

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

Upload behavior:

- The command uploads the current backup folder (`<YYYYmmdd-HHMMSS>`) to the given prefix.
- Resulting path looks like: `s3://my-bucket/hcp-tf-backups/<YYYYmmdd-HHMMSS>/...`
- If you want org-level grouping in remote storage, include it in the prefix:
  `--upload "s3://my-bucket/hcp-tf-backups/<ORG_NAME>"`
- After a successful upload, the local backup folder is automatically removed.
- If upload fails, local files are preserved for retry/troubleshooting.

### Backup + GCS upload

```bash
python3 hcp_tf_backup_restore.py backup \
  --org "<ORG_NAME>" \
  --state-versions 5 \
  --upload "gs://my-bucket/hcp-tf-backups"
```

The same path behavior and local cleanup rules apply to GCS (using `gs://...`).

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

For a full feature-by-feature view, see [Coverage Matrix](#coverage-matrix).

- Historical runs cannot be restored as native run objects. `runs/*.json` (from `--export-runs-history`) is audit-only.
- Sensitive variable values may be unreadable via API and therefore not restorable automatically.
- VCS, OAuth, and agent pool integrations may require manual reconfiguration after restore.
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

