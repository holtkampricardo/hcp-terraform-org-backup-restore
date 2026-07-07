#!/usr/bin/env python3
"""
HCP Terraform organization backup and restore utility.

Backup:
- Exports organization blueprint (projects/workspaces/vars/varsets/tags/team access/teams).
- Downloads latest N finalized state versions per workspace.
- Saves to local directory and optionally uploads to S3/GCS.

Restore:
- Recreates organization/project/workspace scaffolding.
- Restores vars/varsets/tags/remote state consumers.
- Restores state versions by lock -> upload -> poll -> unlock.
- Attempts team and workspace access restoration (best-effort).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests


API_BASE_DEFAULT = "https://app.terraform.io/api/v2"
SCHEMA_VERSION = "1.0"


class HcpApiError(RuntimeError):
    pass


def now_stamp(tz_name: str = "UTC") -> str:
    return dt.datetime.now(ZoneInfo(tz_name)).strftime("%Y%m%d-%H%M%S")


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)


def ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: pathlib.Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_lineage_and_serial_from_state_bytes(state_bytes: bytes) -> Tuple[Optional[str], Optional[int]]:
    try:
        parsed = json.loads(state_bytes.decode("utf-8"))
        lineage = parsed.get("lineage")
        serial = parsed.get("serial")
        return lineage, int(serial) if serial is not None else None
    except Exception:
        return None, None


def parse_iso_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


def get_latest_previous_backup_cutoff(org_backup_root: pathlib.Path, current_backup_dir: pathlib.Path) -> Optional[str]:
    if not org_backup_root.exists():
        return None
    candidates: List[pathlib.Path] = []
    for entry in org_backup_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.resolve() == current_backup_dir.resolve():
            continue
        if (entry / "manifest.json").exists():
            candidates.append(entry)
    if not candidates:
        return None
    latest_prev = sorted(candidates, key=lambda p: p.name, reverse=True)[0]
    try:
        manifest = read_json(latest_prev / "manifest.json")
        return manifest.get("created_at_utc")
    except Exception:
        return None


@dataclass
class HcpTerraformClient:
    token: str
    api_base: str = API_BASE_DEFAULT
    timeout_s: int = 60

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/vnd.api+json",
            }
        )

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        expected: Iterable[int] = (200, 201, 202, 204),
        raw: bool = False,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_base}{path_or_url}"
        resp = self.session.request(method.upper(), url, json=payload, timeout=self.timeout_s)
        if resp.status_code not in set(expected):
            msg = f"{method.upper()} {url} failed [{resp.status_code}]"
            body = resp.text[:2000]
            raise HcpApiError(f"{msg}\n{body}")
        if raw:
            return resp.content
        if resp.status_code == 204:
            return None
        return resp.json()

    def get(self, path_or_url: str) -> Any:
        return self.request("GET", path_or_url)

    def post(self, path_or_url: str, payload: Dict[str, Any], expected: Iterable[int] = (200, 201, 202)) -> Any:
        return self.request("POST", path_or_url, payload=payload, expected=expected)

    def patch(self, path_or_url: str, payload: Dict[str, Any], expected: Iterable[int] = (200, 201, 202)) -> Any:
        return self.request("PATCH", path_or_url, payload=payload, expected=expected)

    def put(self, path_or_url: str, payload: Dict[str, Any], expected: Iterable[int] = (200, 201, 202)) -> Any:
        return self.request("PUT", path_or_url, payload=payload, expected=expected)

    def delete(self, path_or_url: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("DELETE", path_or_url, payload=payload, expected=(200, 202, 204))

    def get_paginated(self, path: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"{self.api_base}{path}" if not path.startswith("http") else path
        while next_url:
            data = self.get(next_url)
            items.extend(data.get("data", []))
            next_url = data.get("links", {}).get("next")
        return items

    def get_org(self, org_name: str) -> Dict[str, Any]:
        return self.get(f"/organizations/{quote_plus(org_name)}")["data"]

    def list_projects(self, org_name: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/organizations/{quote_plus(org_name)}/projects")

    def list_workspaces(self, org_name: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/organizations/{quote_plus(org_name)}/workspaces")

    def show_workspace(self, org_name: str, workspace_name: str) -> Dict[str, Any]:
        return self.get(f"/organizations/{quote_plus(org_name)}/workspaces/{quote_plus(workspace_name)}")["data"]

    def list_workspace_vars(self, workspace_id: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/workspaces/{workspace_id}/vars")

    def list_workspace_tag_bindings(self, workspace_id: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/workspaces/{workspace_id}/tag-bindings")

    def list_workspace_flat_tags(self, workspace_id: str) -> List[Dict[str, Any]]:
        data = self.get(f"/workspaces/{workspace_id}/relationships/tags")
        return data.get("data", [])

    def list_remote_state_consumers(self, workspace_id: str) -> List[Dict[str, Any]]:
        data = self.get(f"/workspaces/{workspace_id}/relationships/remote-state-consumers")
        return data.get("data", [])

    def list_varsets(self, org_name: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/organizations/{quote_plus(org_name)}/varsets")

    def show_varset(self, varset_id: str) -> Dict[str, Any]:
        return self.get(f"/varsets/{varset_id}")["data"]

    def list_varset_vars(self, varset_id: str) -> List[Dict[str, Any]]:
        data = self.get(f"/varsets/{varset_id}/relationships/vars")
        return data.get("data", [])

    def list_teams(self, org_name: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/organizations/{quote_plus(org_name)}/teams")

    def show_team(self, team_id: str) -> Dict[str, Any]:
        return self.get(f"/teams/{team_id}")["data"]

    def list_team_access_for_workspace(self, workspace_id: str) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/team-workspaces?filter%5Bworkspace%5D%5Bid%5D={workspace_id}")

    def list_workspace_runs(self, workspace_id: str, page_size: int = 100) -> List[Dict[str, Any]]:
        return self.get_paginated(f"/workspaces/{workspace_id}/runs?page%5Bsize%5D={page_size}")

    def list_state_versions_for_workspace(self, org_name: str, workspace_name: str) -> List[Dict[str, Any]]:
        ws = quote_plus(workspace_name)
        org = quote_plus(org_name)
        path = (
            f"/state-versions?filter%5Bworkspace%5D%5Bname%5D={ws}"
            f"&filter%5Borganization%5D%5Bname%5D={org}&filter%5Bstatus%5D=finalized"
            "&page%5Bsize%5D=100"
        )
        return self.get_paginated(path)

    def download_raw_state(self, hosted_url: str) -> bytes:
        return self.request("GET", hosted_url, raw=True, expected=(200,))

    def create_org(self, org_name: str, email: str) -> Dict[str, Any]:
        payload = {"data": {"type": "organizations", "attributes": {"name": org_name, "email": email}}}
        return self.post("/organizations", payload)["data"]

    def create_project(self, org_name: str, name: str, description: Optional[str] = None) -> Dict[str, Any]:
        attrs = {"name": name}
        if description:
            attrs["description"] = description
        payload = {"data": {"type": "projects", "attributes": attrs}}
        return self.post(f"/organizations/{quote_plus(org_name)}/projects", payload)["data"]

    def create_workspace(
        self,
        org_name: str,
        name: str,
        attributes: Dict[str, Any],
        relationships: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {"data": {"type": "workspaces", "attributes": {"name": name, **attributes}}}
        if relationships:
            payload["data"]["relationships"] = relationships
        return self.post(f"/organizations/{quote_plus(org_name)}/workspaces", payload)["data"]

    def create_workspace_var(self, workspace_id: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"data": {"type": "vars", "attributes": attributes}}
        return self.post(f"/workspaces/{workspace_id}/vars", payload)["data"]

    def create_varset(self, org_name: str, payload_data: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"data": payload_data}
        return self.post(f"/organizations/{quote_plus(org_name)}/varsets", payload)["data"]

    def patch_workspace(self, workspace_id: str, attributes: Dict[str, Any], relationships: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"data": {"id": workspace_id, "type": "workspaces", "attributes": attributes}}
        if relationships:
            payload["data"]["relationships"] = relationships
        return self.patch(f"/workspaces/{workspace_id}", payload)["data"]

    def replace_remote_state_consumers(self, workspace_id: str, consumer_workspace_ids: List[str]) -> Any:
        payload = {"data": [{"type": "workspaces", "id": w_id} for w_id in consumer_workspace_ids]}
        return self.patch(f"/workspaces/{workspace_id}/relationships/remote-state-consumers", payload)

    def add_flat_tags(self, workspace_id: str, tags: List[str]) -> Any:
        if not tags:
            return None
        payload = {"data": [{"type": "tags", "id": t} for t in tags]}
        return self.post(f"/workspaces/{workspace_id}/relationships/tags", payload, expected=(200, 201, 204))

    def lock_workspace(self, workspace_id: str, reason: str = "Restoring state versions") -> None:
        self.post(f"/workspaces/{workspace_id}/actions/lock", {"reason": reason}, expected=(200, 409))

    def unlock_workspace(self, workspace_id: str) -> None:
        self.post(f"/workspaces/{workspace_id}/actions/unlock", {}, expected=(200, 409, 422, 503))

    def create_state_version(self, workspace_id: str, serial: int, md5: str, lineage: Optional[str]) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {"serial": serial, "md5": md5}
        if lineage:
            attrs["lineage"] = lineage
        payload = {"data": {"type": "state-versions", "attributes": attrs}}
        return self.post(f"/workspaces/{workspace_id}/state-versions", payload)["data"]

    def show_state_version(self, state_version_id: str) -> Dict[str, Any]:
        return self.get(f"/state-versions/{state_version_id}")["data"]

    def upload_state_bytes(self, hosted_upload_url: str, state_bytes: bytes) -> None:
        resp = requests.put(
            hosted_upload_url,
            data=state_bytes,
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.timeout_s,
        )
        if resp.status_code not in (200, 201, 202):
            raise HcpApiError(f"PUT {hosted_upload_url} failed [{resp.status_code}] {resp.text[:1000]}")

    def create_team(self, org_name: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"data": {"type": "teams", "attributes": attributes}}
        return self.post(f"/organizations/{quote_plus(org_name)}/teams", payload)["data"]

    def invite_org_membership(self, org_name: str, email: str, team_ids: List[str]) -> Dict[str, Any]:
        payload = {
            "data": {
                "type": "organization-memberships",
                "attributes": {"email": email},
                "relationships": {"teams": {"data": [{"type": "teams", "id": tid} for tid in team_ids]}},
            }
        }
        return self.post(f"/organizations/{quote_plus(org_name)}/organization-memberships", payload)["data"]

    def add_team_access(self, team_id: str, workspace_id: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "data": {
                "type": "team-workspaces",
                "attributes": attributes,
                "relationships": {
                    "workspace": {"data": {"type": "workspaces", "id": workspace_id}},
                    "team": {"data": {"type": "teams", "id": team_id}},
                },
            }
        }
        return self.post("/team-workspaces", payload)["data"]


def pick_workspace_attributes_for_restore(src: Dict[str, Any]) -> Dict[str, Any]:
    attrs = src.get("attributes", {})
    out: Dict[str, Any] = {}
    for key in (
        "description",
        "terraform-version",
        "auto-apply",
        "auto-apply-run-trigger",
        "allow-destroy-plan",
        "queue-all-runs",
        "speculative-enabled",
        "global-remote-state",
        "execution-mode",
        "working-directory",
        "file-triggers-enabled",
        "trigger-prefixes",
        "trigger-patterns",
        "vcs-repo",
    ):
        if key in attrs and attrs[key] is not None:
            out[key] = attrs[key]
    if out.get("execution-mode") == "agent" and "agent-pool-id" not in out:
        out["execution-mode"] = "remote"
    return out


def backup_command(args: argparse.Namespace) -> int:
    token = args.token or os.getenv("HCP_TF_TOKEN")
    if not token:
        raise SystemExit("Missing token. Use --token or HCP_TF_TOKEN")

    client = HcpTerraformClient(token=token, api_base=args.api_base, timeout_s=args.timeout)
    timestamp = now_stamp(args.backup_timezone)
    org_backup_root = pathlib.Path(args.output_dir).expanduser().resolve() / args.org
    root = org_backup_root / timestamp
    ensure_dir(root)
    ensure_dir(root / "workspaces")
    ensure_dir(root / "projects")
    ensure_dir(root / "varsets")
    ensure_dir(root / "teams")
    ensure_dir(root / "states")
    if args.export_runs_history:
        ensure_dir(root / "runs")

    warnings: List[str] = []
    print(f"[backup] output dir: {root}")
    runs_since_cutoff_utc: Optional[str] = None
    runs_since_cutoff_dt: Optional[dt.datetime] = None
    if args.runs_since_last_backup:
        runs_since_cutoff_utc = get_latest_previous_backup_cutoff(org_backup_root, root)
        runs_since_cutoff_dt = parse_iso_datetime(runs_since_cutoff_utc)
        if runs_since_cutoff_utc:
            print(f"[backup] runs incremental cutoff (UTC): {runs_since_cutoff_utc}")
        else:
            warnings.append("runs_since_last_backup requested but no previous valid backup found; exporting full runs history")

    org = client.get_org(args.org)
    write_json(root / "org.json", org)

    projects = client.list_projects(args.org)
    write_json(root / "projects.json", projects)
    for p in projects:
        p_name = p.get("attributes", {}).get("name", p["id"])
        write_json(root / "projects" / f"{slugify(p_name)}.json", p)

    teams = client.list_teams(args.org)
    write_json(root / "teams.json", teams)
    for t in teams:
        tid = t["id"]
        t_name = t.get("attributes", {}).get("name", tid)
        try:
            t_full = client.show_team(tid)
            write_json(root / "teams" / f"{slugify(t_name)}.json", t_full)
        except Exception as exc:
            warnings.append(f"team {t_name}: unable to fetch detail: {exc}")

    varsets = client.list_varsets(args.org)
    write_json(root / "varsets.json", varsets)
    for v in varsets:
        varset_id = v["id"]
        v_name = v.get("attributes", {}).get("name", varset_id)
        varset_dir = root / "varsets" / slugify(v_name)
        ensure_dir(varset_dir)
        try:
            varset_full = client.show_varset(varset_id)
            write_json(varset_dir / "varset.json", varset_full)
            write_json(varset_dir / "vars.json", client.list_varset_vars(varset_id))
        except Exception as exc:
            warnings.append(f"varset {v_name}: unable to fetch detail/vars: {exc}")

    workspaces = client.list_workspaces(args.org)
    write_json(root / "workspaces.json", workspaces)
    states_index: Dict[str, Any] = {}

    for ws in workspaces:
        ws_name = ws.get("attributes", {}).get("name", ws["id"])
        print(f"[backup] workspace {ws_name}")
        ws_dir = root / "workspaces" / slugify(ws_name)
        ensure_dir(ws_dir)

        try:
            ws_full = client.show_workspace(args.org, ws_name)
            write_json(ws_dir / "workspace.json", ws_full)
            ws_id = ws_full["id"]
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: show failed: {exc}")
            continue

        try:
            write_json(ws_dir / "vars.json", client.list_workspace_vars(ws_id))
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: vars failed: {exc}")

        try:
            write_json(ws_dir / "tag-bindings.json", client.list_workspace_tag_bindings(ws_id))
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: tag-bindings failed: {exc}")

        try:
            write_json(ws_dir / "tags-flat.json", client.list_workspace_flat_tags(ws_id))
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: flat tags failed: {exc}")

        try:
            write_json(ws_dir / "remote-state-consumers.json", client.list_remote_state_consumers(ws_id))
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: remote-state-consumers failed: {exc}")

        try:
            write_json(ws_dir / "team-access.json", client.list_team_access_for_workspace(ws_id))
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: team access failed: {exc}")

        if args.export_runs_history:
            try:
                runs = client.list_workspace_runs(ws_id, page_size=args.runs_page_size)
                if args.runs_since_last_backup and runs_since_cutoff_dt is not None:
                    filtered_runs: List[Dict[str, Any]] = []
                    for run in runs:
                        created_at = run.get("attributes", {}).get("created-at")
                        created_dt = parse_iso_datetime(created_at)
                        if created_dt and created_dt > runs_since_cutoff_dt:
                            filtered_runs.append(run)
                    runs = filtered_runs
                write_json(root / "runs" / f"{slugify(ws_name)}-runs.json", runs)
            except Exception as exc:
                warnings.append(f"workspace {ws_name}: runs history export failed: {exc}")

        ws_state_dir = root / "states" / slugify(ws_name)
        ensure_dir(ws_state_dir)
        downloaded: List[Dict[str, Any]] = []

        try:
            state_versions = client.list_state_versions_for_workspace(args.org, ws_name)
            selected = state_versions[: args.state_versions]
            write_json(ws_state_dir / "state-versions.json", selected)
            for sv in selected:
                attrs = sv.get("attributes", {})
                sv_id = sv["id"]
                dl_url = attrs.get("hosted-state-download-url")
                if not dl_url:
                    warnings.append(f"workspace {ws_name} state {sv_id}: missing hosted-state-download-url")
                    continue
                state_bytes = client.download_raw_state(dl_url)
                state_path = ws_state_dir / f"sv-{sv_id}.tfstate"
                state_path.write_bytes(state_bytes)
                md5 = hashlib.md5(state_bytes).hexdigest()
                lineage, serial_from_file = parse_lineage_and_serial_from_state_bytes(state_bytes)
                downloaded.append(
                    {
                        "id": sv_id,
                        "serial_api": attrs.get("serial"),
                        "serial_file": serial_from_file,
                        "lineage_api": attrs.get("lineage"),
                        "lineage_file": lineage,
                        "md5_api": attrs.get("md5"),
                        "md5_file": md5,
                        "created_at": attrs.get("created-at"),
                        "file": state_path.name,
                    }
                )
        except Exception as exc:
            warnings.append(f"workspace {ws_name}: state backup failed: {exc}")

        states_index[ws_name] = downloaded

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": dt.datetime.utcnow().isoformat() + "Z",
        "org_name": args.org,
        "parameters": {
            "state_versions": args.state_versions,
            "runs_since_last_backup": bool(args.runs_since_last_backup),
            "runs_since_cutoff_utc": runs_since_cutoff_utc,
        },
        "counts": {
            "projects": len(projects),
            "workspaces": len(workspaces),
            "varsets": len(varsets),
            "teams": len(teams),
        },
        "features": {"export_runs_history": bool(args.export_runs_history)},
        "states_index": states_index,
        "warnings": warnings,
    }
    write_json(root / "manifest.json", manifest)
    print(f"[backup] completed with {len(warnings)} warning(s)")

    if args.upload:
        upload_backup(root, args.upload)
        # Keep local backup on upload failures; remove only after successful upload.
        shutil.rmtree(root)
        print(f"[backup] upload successful; removed local backup dir: {root}")

    return 0


def load_workspace_snapshot(backup_dir: pathlib.Path) -> List[Tuple[str, Dict[str, Any]]]:
    ws_entries: List[Tuple[str, Dict[str, Any]]] = []
    for path in sorted((backup_dir / "workspaces").glob("*/workspace.json")):
        ws_dir_name = path.parent.name
        ws_data = read_json(path)
        ws_name = ws_data.get("attributes", {}).get("name", ws_dir_name)
        ws_entries.append((ws_name, ws_data))
    return ws_entries


def restore_command(args: argparse.Namespace) -> int:
    token = args.token or os.getenv("HCP_TF_TOKEN")
    if not token:
        raise SystemExit("Missing token. Use --token or HCP_TF_TOKEN")
    backup_dir = pathlib.Path(args.backup_dir).expanduser().resolve()
    if not backup_dir.exists():
        raise SystemExit(f"Backup dir does not exist: {backup_dir}")

    manifest = read_json(backup_dir / "manifest.json")
    source_org_name = manifest["org_name"]
    target_org_name = args.org_name or source_org_name
    client = HcpTerraformClient(token=token, api_base=args.api_base, timeout_s=args.timeout)
    warnings: List[str] = []

    print("[restore] note: if the original organization name was deleted, HashiCorp may need to re-enable reuse.")
    print(f"[restore] source org={source_org_name}, target org={target_org_name}")

    org_exists = True
    try:
        client.get_org(target_org_name)
    except Exception:
        org_exists = False

    if org_exists and not args.allow_non_empty_target:
        existing_projects = client.list_projects(target_org_name)
        existing_workspaces = client.list_workspaces(target_org_name)
        existing_varsets = client.list_varsets(target_org_name)
        existing_teams = client.list_teams(target_org_name)

        non_default_projects = [
            p for p in existing_projects if p.get("attributes", {}).get("name", "").lower() != "default project"
        ]
        non_owners_teams = [
            t for t in existing_teams if t.get("attributes", {}).get("name", "").lower() != "owners"
        ]

        has_existing_content = bool(existing_workspaces or existing_varsets or non_default_projects or non_owners_teams)
        if has_existing_content:
            raise SystemExit(
                "Safety check failed: target organization is not empty.\n"
                f"- workspaces: {len(existing_workspaces)}\n"
                f"- varsets: {len(existing_varsets)}\n"
                f"- non-default projects: {len(non_default_projects)}\n"
                f"- non-owners teams: {len(non_owners_teams)}\n\n"
                "Restore is intended for rebuild-from-scratch by default.\n"
                "If you intentionally want to restore into an existing non-empty org, use --allow-non-empty-target."
            )

    if args.create_org and not org_exists:
        org_email = args.org_email
        if not org_email:
            org_json = read_json(backup_dir / "org.json")
            org_email = org_json.get("attributes", {}).get("email")
        if not org_email:
            raise SystemExit("Missing org email for create-org. Pass --org-email.")
        client.create_org(target_org_name, org_email)
        print(f"[restore] created org {target_org_name}")

    # Build existing maps
    existing_projects = {p.get("attributes", {}).get("name"): p for p in client.list_projects(target_org_name)}
    existing_workspaces = {w.get("attributes", {}).get("name"): w for w in client.list_workspaces(target_org_name)}
    existing_teams = {t.get("attributes", {}).get("name"): t for t in client.list_teams(target_org_name)}

    project_name_to_id: Dict[str, str] = {}
    workspace_name_to_id: Dict[str, str] = {}
    team_name_to_id: Dict[str, str] = {}

    # Projects
    source_projects = read_json(backup_dir / "projects.json")
    for p in source_projects:
        p_name = p.get("attributes", {}).get("name")
        if not p_name:
            continue
        p_id: Optional[str] = None
        if p_name in existing_projects:
            p_id = existing_projects[p_name]["id"]
        else:
            desc = p.get("attributes", {}).get("description")
            try:
                created = client.create_project(target_org_name, p_name, desc)
                p_id = created["id"]
            except Exception as exc:
                warnings.append(f"project {p_name}: create failed: {exc}")
        if p_id:
            project_name_to_id[p_name] = p_id

    # Workspaces
    for ws_name, ws_data in load_workspace_snapshot(backup_dir):
        ws_id: Optional[str] = None
        if ws_name in existing_workspaces:
            ws_id = existing_workspaces[ws_name]["id"]
        else:
            attrs = pick_workspace_attributes_for_restore(ws_data)
            rels: Dict[str, Any] = {}
            project_rel = ws_data.get("relationships", {}).get("project", {}).get("data")
            if project_rel and project_rel.get("id"):
                src_prj_id = project_rel["id"]
                src_prj_name = None
                for p in source_projects:
                    if p.get("id") == src_prj_id:
                        src_prj_name = p.get("attributes", {}).get("name")
                        break
                if src_prj_name and src_prj_name in project_name_to_id:
                    rels["project"] = {"data": {"type": "projects", "id": project_name_to_id[src_prj_name]}}
            try:
                created = client.create_workspace(target_org_name, ws_name, attrs, rels if rels else None)
                ws_id = created["id"]
            except Exception as exc:
                warnings.append(f"workspace {ws_name}: create failed: {exc}")
        if ws_id:
            workspace_name_to_id[ws_name] = ws_id

    # Variable sets
    source_varsets = read_json(backup_dir / "varsets.json")
    varset_name_to_id: Dict[str, str] = {}
    existing_varsets = {v.get("attributes", {}).get("name"): v for v in client.list_varsets(target_org_name)}

    for v in source_varsets:
        v_name = v.get("attributes", {}).get("name")
        if not v_name:
            continue
        if v_name in existing_varsets and args.skip_existing:
            varset_name_to_id[v_name] = existing_varsets[v_name]["id"]
            continue

        src_v_dir = backup_dir / "varsets" / slugify(v_name)
        src_v_full = read_json(src_v_dir / "varset.json") if (src_v_dir / "varset.json").exists() else v
        src_v_vars = read_json(src_v_dir / "vars.json") if (src_v_dir / "vars.json").exists() else []
        attrs = src_v_full.get("attributes", {})

        payload_data: Dict[str, Any] = {
            "type": "varsets",
            "attributes": {
                "name": v_name,
                "description": attrs.get("description") or "",
                "global": bool(attrs.get("global", False)),
                "priority": bool(attrs.get("priority", False)),
            },
            "relationships": {},
        }
        # Parent mapping
        parent = src_v_full.get("relationships", {}).get("parent", {}).get("data")
        if parent and parent.get("type") == "projects":
            src_project_id = parent.get("id")
            src_project_name = None
            for p in source_projects:
                if p.get("id") == src_project_id:
                    src_project_name = p.get("attributes", {}).get("name")
                    break
            if src_project_name and src_project_name in project_name_to_id:
                payload_data["relationships"]["parent"] = {
                    "data": {"type": "projects", "id": project_name_to_id[src_project_name]}
                }

        # Vars in varset
        vars_payload = []
        for var in src_v_vars:
            va = var.get("attributes", {})
            if va.get("sensitive") and (va.get("value") is None):
                warnings.append(f"varset {v_name} sensitive var {va.get('key')} has no value in backup")
            vars_payload.append(
                {
                    "type": "vars",
                    "attributes": {
                        "key": va.get("key"),
                        "value": va.get("value", ""),
                        "category": va.get("category", "terraform"),
                        "hcl": bool(va.get("hcl", False)),
                        "sensitive": bool(va.get("sensitive", False)),
                        "description": va.get("description", ""),
                    },
                }
            )
        if vars_payload:
            payload_data["relationships"]["vars"] = {"data": vars_payload}

        # Workspace assignments
        ws_rels = src_v_full.get("relationships", {}).get("workspaces", {}).get("data", [])
        ws_assignment = []
        src_workspaces = read_json(backup_dir / "workspaces.json")
        src_ws_id_to_name = {w["id"]: w.get("attributes", {}).get("name") for w in src_workspaces}
        for ws_ref in ws_rels:
            src_name = src_ws_id_to_name.get(ws_ref.get("id"))
            if src_name and src_name in workspace_name_to_id:
                ws_assignment.append({"type": "workspaces", "id": workspace_name_to_id[src_name]})
        if ws_assignment:
            payload_data["relationships"]["workspaces"] = {"data": ws_assignment}

        try:
            created = client.create_varset(target_org_name, payload_data)
            varset_name_to_id[v_name] = created["id"]
        except Exception as exc:
            warnings.append(f"varset {v_name}: create failed: {exc}")

    # Workspace vars/tags/remote consumers/state/team access
    src_workspaces = read_json(backup_dir / "workspaces.json")
    src_ws_id_to_name = {w["id"]: w.get("attributes", {}).get("name") for w in src_workspaces}

    for ws_name, ws_data in load_workspace_snapshot(backup_dir):
        target_ws_id = workspace_name_to_id.get(ws_name)
        if not target_ws_id:
            warnings.append(f"workspace {ws_name}: missing in target, skipping workspace-level restore")
            continue
        ws_dir = backup_dir / "workspaces" / slugify(ws_name)

        # Workspace vars
        vars_path = ws_dir / "vars.json"
        if vars_path.exists():
            for var in read_json(vars_path):
                va = var.get("attributes", {})
                if va.get("sensitive") and (va.get("value") is None):
                    warnings.append(f"workspace {ws_name} sensitive var {va.get('key')} has no value in backup")
                    continue
                payload = {
                    "key": va.get("key"),
                    "value": va.get("value", ""),
                    "description": va.get("description"),
                    "category": va.get("category", "terraform"),
                    "hcl": bool(va.get("hcl", False)),
                    "sensitive": bool(va.get("sensitive", False)),
                }
                try:
                    client.create_workspace_var(target_ws_id, payload)
                except Exception as exc:
                    warnings.append(f"workspace {ws_name} var {payload.get('key')}: create failed: {exc}")

        # Enhanced tags
        tag_bindings_path = ws_dir / "tag-bindings.json"
        if tag_bindings_path.exists():
            tags_data = read_json(tag_bindings_path)
            bindings = []
            for t in tags_data:
                attrs = t.get("attributes", {})
                if attrs.get("key"):
                    bindings.append({"type": "tag-bindings", "attributes": {"key": attrs["key"], "value": attrs.get("value", "")}})
            if bindings:
                try:
                    client.patch_workspace(target_ws_id, attributes={}, relationships={"tag-bindings": {"data": bindings}})
                except Exception as exc:
                    warnings.append(f"workspace {ws_name}: restore tag-bindings failed: {exc}")

        # Flat tags
        flat_tags_path = ws_dir / "tags-flat.json"
        if flat_tags_path.exists():
            flat_tags = [t.get("id") for t in read_json(flat_tags_path) if t.get("id")]
            try:
                client.add_flat_tags(target_ws_id, flat_tags)
            except Exception as exc:
                warnings.append(f"workspace {ws_name}: restore flat tags failed: {exc}")

        # Remote state consumers
        rsc_path = ws_dir / "remote-state-consumers.json"
        if rsc_path.exists():
            src_consumers = read_json(rsc_path)
            mapped_consumer_ids = []
            for c in src_consumers:
                src_consumer_name = src_ws_id_to_name.get(c.get("id"))
                if src_consumer_name and src_consumer_name in workspace_name_to_id:
                    mapped_consumer_ids.append(workspace_name_to_id[src_consumer_name])
            if mapped_consumer_ids:
                try:
                    client.replace_remote_state_consumers(target_ws_id, mapped_consumer_ids)
                except Exception as exc:
                    warnings.append(f"workspace {ws_name}: restore remote-state-consumers failed: {exc}")

        # Restore states
        if args.restore_states:
            ws_state_dir = backup_dir / "states" / slugify(ws_name)
            sv_index_path = ws_state_dir / "state-versions.json"
            if sv_index_path.exists():
                sv_index = read_json(sv_index_path)
                # oldest -> newest
                def _sort_key(x: Dict[str, Any]) -> Tuple[int, str]:
                    attrs = x.get("attributes", {})
                    serial = attrs.get("serial")
                    created_at = attrs.get("created-at") or ""
                    return (serial if isinstance(serial, int) else -1, created_at)

                sorted_sv = sorted(sv_index, key=_sort_key)
                client.lock_workspace(target_ws_id)
                for sv in sorted_sv:
                    attrs = sv.get("attributes", {})
                    sv_id = sv.get("id")
                    state_file = ws_state_dir / f"sv-{sv_id}.tfstate"
                    if not state_file.exists():
                        warnings.append(f"workspace {ws_name} state {sv_id}: missing local tfstate file")
                        continue
                    state_bytes = state_file.read_bytes()
                    md5 = hashlib.md5(state_bytes).hexdigest()
                    serial = attrs.get("serial")
                    if serial is None:
                        _, serial = parse_lineage_and_serial_from_state_bytes(state_bytes)
                    lineage = attrs.get("lineage")
                    if serial is None:
                        warnings.append(f"workspace {ws_name} state {sv_id}: missing serial, skipped")
                        continue
                    try:
                        created_sv = client.create_state_version(target_ws_id, int(serial), md5, lineage)
                        upload_url = created_sv.get("attributes", {}).get("hosted-state-upload-url")
                        new_sv_id = created_sv.get("id")
                        if not upload_url:
                            warnings.append(f"workspace {ws_name}: created state version {new_sv_id} has no upload URL")
                            continue
                        client.upload_state_bytes(upload_url, state_bytes)
                        for _ in range(args.state_poll_attempts):
                            time.sleep(args.state_poll_interval_s)
                            status = client.show_state_version(new_sv_id).get("attributes", {}).get("status")
                            if status == "finalized":
                                break
                        else:
                            warnings.append(f"workspace {ws_name}: state version {new_sv_id} not finalized in time")
                    except Exception as exc:
                        warnings.append(f"workspace {ws_name} state {sv_id}: restore failed: {exc}")
                client.unlock_workspace(target_ws_id)

    # Teams and permissions (best-effort)
    if args.restore_perms:
        source_teams = read_json(backup_dir / "teams.json") if (backup_dir / "teams.json").exists() else []
        for t in source_teams:
            t_name = t.get("attributes", {}).get("name")
            if not t_name:
                continue
            if t_name in existing_teams:
                team_name_to_id[t_name] = existing_teams[t_name]["id"]
                continue
            attrs = t.get("attributes", {})
            t_payload = {
                "name": t_name,
                "visibility": attrs.get("visibility", "secret"),
            }
            if "organization-access" in attrs:
                t_payload["organization-access"] = attrs.get("organization-access")
            try:
                created = client.create_team(target_org_name, t_payload)
                team_name_to_id[t_name] = created["id"]
            except Exception as exc:
                warnings.append(f"team {t_name}: create failed: {exc}")

        # Team access by workspace
        for ws_name in workspace_name_to_id:
            ws_dir = backup_dir / "workspaces" / slugify(ws_name)
            team_access_path = ws_dir / "team-access.json"
            if not team_access_path.exists():
                continue
            target_ws_id = workspace_name_to_id[ws_name]
            for ta in read_json(team_access_path):
                attrs = ta.get("attributes", {})
                team_ref = ta.get("relationships", {}).get("team", {}).get("data", {})
                src_team_id = team_ref.get("id")
                if not src_team_id:
                    continue
                src_team_name = None
                for t in source_teams:
                    if t.get("id") == src_team_id:
                        src_team_name = t.get("attributes", {}).get("name")
                        break
                if not src_team_name:
                    continue
                tgt_team_id = team_name_to_id.get(src_team_name) or existing_teams.get(src_team_name, {}).get("id")
                if not tgt_team_id:
                    warnings.append(f"workspace {ws_name}: team {src_team_name} not found for access restore")
                    continue
                add_attrs = {"access": attrs.get("access", "read")}
                for k in ("runs", "variables", "state-versions", "sentinel-mocks", "workspace-locking", "run-tasks"):
                    if k in attrs and attrs[k] is not None:
                        add_attrs[k] = attrs[k]
                try:
                    client.add_team_access(tgt_team_id, target_ws_id, add_attrs)
                except Exception as exc:
                    warnings.append(f"workspace {ws_name}: add team access {src_team_name} failed: {exc}")

        # Optional invitations file
        invites_file = backup_dir / "organization-memberships.json"
        if invites_file.exists():
            memberships = read_json(invites_file)
            for mem in memberships:
                email = mem.get("attributes", {}).get("email")
                if not email:
                    continue
                team_ids: List[str] = []
                for t_ref in mem.get("relationships", {}).get("teams", {}).get("data", []):
                    src_team_id = t_ref.get("id")
                    src_team_name = None
                    for t in source_teams:
                        if t.get("id") == src_team_id:
                            src_team_name = t.get("attributes", {}).get("name")
                            break
                    if src_team_name and src_team_name in team_name_to_id:
                        team_ids.append(team_name_to_id[src_team_name])
                if team_ids:
                    try:
                        client.invite_org_membership(target_org_name, email, team_ids)
                    except Exception as exc:
                        warnings.append(f"invite {email} failed: {exc}")

    write_json(backup_dir / "restore-report.json", {"warnings": warnings, "target_org_name": target_org_name})
    print(f"[restore] done with {len(warnings)} warning(s). See {backup_dir / 'restore-report.json'}")
    return 0


def upload_backup(backup_root: pathlib.Path, destination: str) -> None:
    if destination.startswith("s3://"):
        cmd = ["aws", "s3", "cp", str(backup_root), destination.rstrip("/") + "/" + backup_root.name + "/", "--recursive"]
    elif destination.startswith("gs://"):
        cmd = ["gsutil", "-m", "cp", "-r", str(backup_root), destination.rstrip("/") + "/"]
    else:
        raise SystemExit("Unsupported --upload target. Use s3://... or gs://...")
    print(f"[upload] running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HCP Terraform org backup/restore utility")
    parser.add_argument("--api-base", default=API_BASE_DEFAULT, help="HCP Terraform API base URL")
    parser.add_argument("--token", default=None, help="API token (fallback: HCP_TF_TOKEN env var)")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")

    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Create organization backup")
    p_backup.add_argument("--org", required=True, help="Organization name")
    p_backup.add_argument("--output-dir", default="./backups", help="Local root directory for backups")
    p_backup.add_argument("--backup-timezone", default="UTC", help='Timezone used in backup folder timestamp (for example: "UTC", "Europe/Madrid")')
    p_backup.add_argument("--state-versions", type=int, default=5, help="How many latest finalized state versions per workspace")
    p_backup.add_argument("--export-runs-history", action="store_true", help="Export workspace runs history to backup/runs/*.json")
    p_backup.add_argument("--runs-since-last-backup", action="store_true", help="When exporting runs history, include only runs newer than previous backup cutoff")
    p_backup.add_argument("--runs-page-size", type=int, default=100, help="Page size for workspace runs export pagination")
    p_backup.add_argument("--upload", default=None, help="Optional upload target: s3://bucket/prefix or gs://bucket/prefix")
    p_backup.set_defaults(func=backup_command)

    p_restore = sub.add_parser("restore", help="Restore from existing backup directory")
    p_restore.add_argument("--backup-dir", required=True, help="Backup directory containing manifest.json")
    p_restore.add_argument("--org-name", default=None, help="Override target org name (default: source org from manifest)")
    p_restore.add_argument("--create-org", action="store_true", default=True, help="Create org if missing")
    p_restore.add_argument("--org-email", default=None, help="Admin email used if org is created")
    p_restore.add_argument("--skip-existing", action="store_true", default=True, help="Skip entities already existing")
    p_restore.add_argument("--restore-states", action="store_true", default=True, help="Restore state versions")
    p_restore.add_argument("--restore-perms", action="store_true", default=True, help="Restore teams/access best-effort")
    p_restore.add_argument(
        "--allow-non-empty-target",
        action="store_true",
        default=False,
        help="Allow restore into an existing organization that already contains resources (unsafe, use with care)",
    )
    p_restore.add_argument("--state-poll-attempts", type=int, default=20, help="Max polls per uploaded state version")
    p_restore.add_argument("--state-poll-interval-s", type=int, default=3, help="Poll interval for state finalization")
    p_restore.set_defaults(func=restore_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except HcpApiError as exc:
        print(f"[error] API failure: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"[error] command failed: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("[error] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
