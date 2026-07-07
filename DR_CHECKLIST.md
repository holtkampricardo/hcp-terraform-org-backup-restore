# DR Checklist - HCP Terraform Backup/Restore

## 1) Preparation (before incident)

- Validate `HCP_TF_TOKEN` and required permissions:
  - read/write state versions
  - lock/unlock workspace
  - manage workspaces/projects/variables
- Schedule recurring backups:
  - `python3 hcp_tf_backup_restore.py backup --org "<ORG>" --state-versions 5 --export-runs-history`
- Define backup storage strategy:
  - local only, or local + `--upload s3://...` / `--upload gs://...`
- Periodically verify:
  - `manifest.json` exists
  - `states/<workspace>/sv-*.tfstate` exists for critical workspaces
  - `runs/*.json` exists when run-history export is enabled

## 2) Incident response (deleted org/workspaces)

- Identify latest valid backup:
  - `LATEST="$(ls -1t backups/<ORG> | head -n 1)"`
- If org was deleted and you need the same org name:
  - open a HashiCorp support ticket to re-enable org-name reuse.
- Run restore:
  - Same org name:
    - `python3 hcp_tf_backup_restore.py restore --backup-dir "backups/<ORG>/$LATEST" --org-name "<ORG>"`
  - New org name:
    - `python3 hcp_tf_backup_restore.py restore --backup-dir "backups/<ORG>/$LATEST" --org-name "<TARGET_ORG>" --org-email "admin@example.com"`

## 3) Post-restore validation

- Check restore output:
  - `cat "backups/<ORG>/$LATEST/restore-report.json"`
- Validate in HCP Terraform:
  - projects and workspaces recreated
  - variables and variable sets restored
  - state available in critical workspaces (`States` tab)
  - teams and workspace access restored (best-effort)
- Trigger validation runs for critical workspaces.

## 4) Expected limitations

- Historical runs cannot be restored as native run records in HCP Terraform.
- `--export-runs-history` exports audit data only (`runs/*.json`), not restorable run objects.
- Sensitive variable values may be absent via API and require manual re-entry.
- Some VCS/OAuth/agent-pool integrations can require manual reconfiguration.

