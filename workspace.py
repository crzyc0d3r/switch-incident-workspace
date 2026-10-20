"""A deterministic incident replay and a client for a real local Switch workspace."""

from __future__ import annotations

import argparse
import ast
import copy
import html
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml

ROOT = Path(__file__).resolve().parent
LOCK = json.loads((ROOT / "upstream.lock.json").read_text(encoding="utf-8"))


def load_scenario(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    incidents = data["incidents"]
    if len(incidents) != 2 or {i["service"] for i in incidents} != {"inference", "training"}:
        raise ValueError("The exercise needs one inference and one training incident")
    ids = [i["id"] for i in incidents]
    if len(set(ids)) != len(ids):
        raise ValueError("Incident ids must be unique")
    configs = data["configs"]
    revisions = {value["revision"] for value in configs.values()}
    if len(revisions) != len(configs):
        raise ValueError("Each configuration must have a distinct revision")
    for incident in incidents:
        if incident["config_revision"] not in revisions:
            raise ValueError(f"Unknown configuration on {incident['id']}")
        if incident["service"] == "training":
            if incident["duration_hours"] <= 0 or incident["retry_interval_seconds"] <= 0:
                raise ValueError("Training duration and retry interval must be positive")
    return data


def probe(service: str, config: dict[str, Any], healthy_endpoint: str,
          *, duration_hours: float = 6, retry_seconds: int = 60) -> dict[str, Any]:
    """Simulate dependency resolution from a fixture; never make network requests."""
    if service not in {"inference", "training"}:
        raise ValueError(f"Unsupported service: {service}")
    available = config["endpoint"] == healthy_endpoint
    result = {
        "service": service, "revision": config["revision"],
        "endpoint": config["endpoint"], "host": urlparse(config["endpoint"]).hostname,
        "dependency_available": available,
    }
    if service == "inference":
        # The fixture's service maps its unhandled feature lookup failure to 500.
        result.update(http_status=200 if available else 500, error=None if available else "NXDOMAIN")
    else:
        result.update(
            job_status="COMPLETED" if available else "RUNNING",
            completed_batches=1 if available else 0,
            retry_attempts=0 if available else int(duration_hours * 3600 // retry_seconds),
            stalled_hours=0 if available else duration_hours,
            error=None if available else "NXDOMAIN",
        )
    return result


def replay(data: dict[str, Any], human_context: list[str] | None = None) -> dict[str, Any]:
    """Collect two room investigations, cross-room correlation and QA controls."""
    configs = {c["revision"]: c for c in data["configs"].values()}
    current, rollback, transcript = {}, {}, []
    for incident in data["incidents"]:
        room = incident["room"]
        transcript.append({"room": "control", "role": "Scenario Manager", "action": "create_room", "target": room})
        kwargs = {"duration_hours": incident.get("duration_hours", 6),
                  "retry_seconds": incident.get("retry_interval_seconds", 60)}
        current[incident["id"]] = probe(
            incident["service"], configs[incident["config_revision"]], data["healthy_endpoint"], **kwargs)
        rollback[incident["id"]] = probe(
            incident["service"], data["configs"]["previous"], data["healthy_endpoint"], **kwargs)
        transcript.append({"room": room, "role": "Software Engineer", "action": "probe",
                           "evidence": [e["id"] for e in incident["evidence"]],
                           "result": current[incident["id"]]})
    for source, target in ((0, 1), (1, 0)):
        transcript.append({"room": data["incidents"][source]["room"], "role": "Scenario Manager",
                           "action": "link_room", "target": data["incidents"][target]["room"]})
    context = [{"room": "control", "role": "Human", "action": "context", "verified": False, "text": text}
               for text in human_context or []]
    transcript.extend(context)
    revisions = {i["config_revision"] for i in data["incidents"]}
    hosts = {p["host"] for p in current.values()}
    both_fail = all(not p["dependency_available"] for p in current.values())
    both_recover = all(p["dependency_available"] for p in rollback.values())
    matching_logs = all(
        any(e["kind"] == "error" and current[i["id"]]["host"] in e["detail"] for e in i["evidence"])
        for i in data["incidents"]
    )
    changes = [c for c in data["changes"] if c["kind"] == "shared-config" and c["after"] in revisions]
    shared_change = len(revisions) == 1 and len(changes) == 1
    confirmed = shared_change and len(hosts) == 1 and both_fail and both_recover and matching_logs
    # Hold config constant and undo only the inference image change.
    inference = next(i for i in data["incidents"] if i["service"] == "inference")
    image_change = next((c for c in data["changes"] if c["kind"] == "inference-image"), None)
    old_image_control = copy.deepcopy(current[inference["id"]])
    old_image_control["image"] = image_change["before"] if image_change else inference["image"]
    old_image_control["note"] = "Same fixture failure model with old image and unchanged bad configuration"
    checks = {"same_effective_revision": len(revisions) == 1, "same_failing_host": len(hosts) == 1,
              "both_fail": both_fail, "both_recover_on_config_rollback": both_recover,
              "error_logs_match_probe_hosts": matching_logs, "one_shared_config_push": shared_change}
    evidence = [e["id"] for i in data["incidents"] for e in i["evidence"]] + [c["id"] for c in changes]
    transcript.append({"room": "control", "role": "Architect", "action": "correlate",
                       "checks": checks, "evidence": evidence})
    transcript.append({"room": "control", "role": "QA", "action": "counterfactuals",
                       "config_rollback": rollback, "old_image_bad_config": old_image_control})
    conclusion = (
        f"Shared configuration {next(iter(revisions))} points both services at an unresolvable feature-store host. "
        "Inference returns HTTP 500; training retries indefinitely and makes no progress."
        if confirmed else "Evidence is insufficient to confirm a shared configuration cause. Investigate separately."
    )
    report = {
        "mode": "deterministic offline simulation", "scenario": data["name"],
        "clock": data["clock"], "status": "confirmed_in_simulation" if confirmed else "inconclusive",
        "root_cause": conclusion, "evidence": evidence, "checks": checks,
        "current_probes": current, "config_rollback_probes": rollback,
        "old_image_control": old_image_control, "human_context": context,
        "mitigation": ["Revert the shared endpoint to the last known-good value after human review.",
                       "Confirm inference returns 200 and training completes batches; keep images unchanged."],
        "prevention": ["Validate feature-store host resolution before promoting shared configuration.",
                       "Bound training retries and alert on missing progress, not only job liveness.",
                       "Return explicit dependency failures and monitor inference error rates."],
        "limitations": ["Resolution and service behavior are fixture models; no real DNS, HTTP or ML job was run.",
                        "The old-image control assumes unchanged feature-lookup behavior; inspect actual images in a real incident.",
                        "Human statements are preserved as unverified context and never override measured checks."],
    }
    transcript.append({"room": "control", "role": "Scenario Manager", "action": "consolidated_rca",
                       "status": report["status"], "text": conclusion})
    report["transcript"] = transcript
    return report


def write_reports(report: dict[str, Any], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(event.get(k, '')))}</td>" for k in ("room", "role", "action"))
        + f"<td><pre>{html.escape(json.dumps(event, indent=2))}</pre></td></tr>"
        for event in report["transcript"])
    document = ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Incident exercise RCA</title>"
                "<style>body{font:16px system-ui;max-width:1100px;margin:3rem auto;padding:0 1rem;color:#18323e}"
                "table{border-collapse:collapse;width:100%}td,th{border:1px solid #bad0d9;padding:.6rem;text-align:left}"
                "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}small{color:#536b77}</style>"
                f"<h1>{html.escape(report['scenario'])}</h1><small>{html.escape(report['mode'])}</small>"
                f"<h2>{html.escape(report['status'])}</h2><p>{html.escape(report['root_cause'])}</p>"
                "<h2>Room evidence and handoffs</h2><table><thead><tr><th>Room</th><th>Role</th><th>Action</th>"
                f"<th>Details</th></tr></thead><tbody>{rows}</tbody></table>"
                "<h2>Limits</h2><ul>" + "".join(f"<li>{html.escape(s)}</li>" for s in report["limitations"])
                + "</ul></html>")
    (out / "report.html").write_text(document, encoding="utf-8")


def incident_inputs(prefix: str, bridge: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", prefix):
        raise ValueError("Prefix must contain lowercase letters, digits and hyphens")
    if not bridge.strip():
        raise ValueError("Specify the local Mattermost bridge display name")
    return {"prefix": prefix, "bridge": bridge,
            "manager_agent": f"{prefix}-scenario-manager", "engineer_agent": f"{prefix}-engineer",
            "architect_agent": f"{prefix}-architect", "qa_agent": f"{prefix}-qa"}


class NoRedirects(HTTPRedirectHandler):
    """Keep an agent credential on the exact local endpoint it was issued for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SwitchClient:
    """HTTP-up portion of the actual agent protocol; no invented agent endpoints."""

    def __init__(self, base_url: str, agent_id: str, api_key: str):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("This demo client only provisions a local HTTP Switch instance")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a plain local bridge URL without credentials or query parameters")
        if not agent_id or not api_key:
            raise ValueError("Set SWITCH_AGENT_ID and SWITCH_AGENT_API_KEY for an existing owned manager agent")
        self.base_url, self.agent_id, self.api_key = base_url.rstrip("/"), agent_id, api_key

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = Request(self.base_url + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({}), NoRedirects).open(req, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            # A server might echo an incoming request: do not print its body or headers.
            raise RuntimeError(f"Switch returned HTTP {exc.code} for {method} {path}") from None
        except URLError:
            raise RuntimeError("Local Switch bridge could not be reached") from None

    def provision(self, template: str, inputs: dict[str, str]) -> dict[str, Any]:
        base = f"/agents/{quote(self.agent_id, safe='')}/ops"
        operations = self.request("GET", base)["operations"]
        name = "create_room_from_yaml"
        if name not in operations:
            raise RuntimeError("The server does not advertise create_room_from_yaml")
        schema = operations[name]["input_schema"]
        properties = schema.get("properties", {})
        if not {"yaml", "inputs"} <= set(properties) or set(schema.get("required", [])) - {"yaml", "inputs"}:
            raise RuntimeError("The server's provisioning schema differs from the pinned contract")
        response = self.request("POST", f"{base}/{name}", {"yaml": template, "inputs": inputs})
        result = response["result"]
        if result.get("errors") or any(r.get("failed_attachments") for r in result.get("rooms", [])):
            raise RuntimeError("Provisioning was partial; inspect the local Switch room list before retrying")
        if len(result.get("rooms", [])) != 2:
            raise RuntimeError("Provisioning did not return the expected two incident rooms")
        return result


def check_source(source: Path) -> None:
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if sha != LOCK["commit"]:
        raise ValueError(f"Expected pinned Switch commit {LOCK['commit']}; found {sha}")


def validate_upstream(source: Path) -> dict[str, Any]:
    """Run the pinned compiler, agent planner, room parser and role-name validator."""
    check_source(source)
    sys.path.insert(0, str(source / "core"))
    from switch_core.agent_templates import plan_template
    from switch_core.db.stores.room_role_store import validate_role_name
    from switch_core.rooms_yaml import GroupSpec, RoomYamlService
    from switch_core.template_v2 import compile_v2

    builtins = {"$creator": "operator", "$creator_email": "operator@example.invalid",
                "$agents_dir": "C:/agents", "$date": "2026-01-15", "$timestamp": "test"}
    team_text = (ROOT / "templates/team.template.yaml").read_text(encoding="utf-8")
    team_inputs = {"prefix": "incident-lab", "bridge": "Local Mattermost", "provider": "codex",
                   "machine": "local", "project_directory": str(ROOT)}
    machines = [{"id": "local", "name": "local", "state": "online", "agents_dir": "C:/agents",
                 "providers": [{"provider": "codex", "installed": True, "auth": "ready"}]}]
    team = plan_template(team_text, {}, team_inputs, builtins=builtins, machines=machines)
    if len(team.new_agents) != 4 or team.document is None:
        raise ValueError("The upstream agent planner did not produce four agents and a control room")
    parser = RoomYamlService.__new__(RoomYamlService)  # Parsing uses no DB/service dependencies.
    control = parser.parse_template(team.document, inputs=team.inputs, builtins=builtins)
    text = (ROOT / "templates/incident-rooms.template.yaml").read_text(encoding="utf-8")
    compiled = compile_v2(text, incident_inputs("incident-lab", "Local Mattermost"), builtins)
    incidents = parser.parse_template(compiled.document, inputs=compiled.inputs, builtins=builtins)
    if not isinstance(incidents.spec, GroupSpec) or len(incidents.spec.rooms) != 2 or len(incidents.spec.links) != 2:
        raise ValueError("The upstream parser did not produce two rooms and two directed links")
    for room in incidents.spec.rooms:
        for role in room.roles:
            validate_role_name(role.name)
    # Check the actual implementation signature without importing the full server operation registry.
    definitions = source / "core/switch_core/bridges/agent/operations/definitions.py"
    tree = ast.parse(definitions.read_text(encoding="utf-8"))
    signatures = {n.name: [a.arg for a in n.args.args] for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    if signatures.get("create_room_from_yaml") != ["yaml", "inputs"]:
        raise ValueError("The upstream create_room_from_yaml signature changed")
    if signatures.get("link_rooms") != ["source_room_id", "target_room_id", "label"]:
        raise ValueError("The upstream link_rooms signature changed")
    return {"commit": LOCK["commit"], "agent_plans": len(team.new_agents), "control_room": control.spec.name,
            "incident_rooms": len(incidents.spec.rooms), "directed_links": len(incidents.spec.links),
            "role_definitions": sum(len(r.roles) for r in incidents.spec.rooms), "operation_signatures": "matched"}


def prepare(runtime: Path, source: Path | None) -> Path:
    """Fetch pinned source and generate only local, ignored service credentials."""
    runtime.mkdir(parents=True, exist_ok=True)
    source = source or runtime / "switch"
    if not source.exists():
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--no-checkout", "--depth", "1",
                        LOCK["repository"], str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["git", "-C", str(source), "fetch", "--depth", "1", "origin", LOCK["commit"]], check=True)
        subprocess.run(["git", "-C", str(source), "checkout", "--detach", LOCK["commit"]], check=True)
    check_source(source)
    env_path = runtime / "stack.env"
    if not env_path.exists():
        values = {key: secrets.token_hex(24) for key in (
            "DB_PASSWORD", "DB_OWNER_PASSWORD", "AGENT_REGISTRATION_TOKEN", "GATEWAY_ADMIN_PASSWORD",
            "MATTERMOST_ADMIN_PASSWORD", "MATTERMOST_USER_PASSWORD", "CONTROLLER_TOKEN_SECRET")}
        values.update(SECRET_KEYS="local:" + secrets.token_hex(32), SWITCH_VERSION="source-" + LOCK["commit"][:12],
                      SWITCH_BIND_ADDR="127.0.0.1", API_HOST_PORT="18000", GATEWAY_HOST_PORT="13000",
                      MATTERMOST_HOST_PORT="18065", POSTGRES_HOST_PORT="15432", TELEMETRY_ENABLED="false",
                      THIRD_PARTY_AVATARS_ENABLED="false", FEATURE_FLAGS_ENABLED="agent_management",
                      GATEWAY_PUBLIC_URL="http://localhost:18000", FRONTEND_BASE_URL="http://localhost:13000")
        text = (source / ".env.example").read_text(encoding="utf-8")
        for key, value in values.items():
            pattern = rf"(?m)^{re.escape(key)}=.*$"
            text = re.sub(pattern, f"{key}={value}", text) if re.search(pattern, text) else text + f"\n{key}={value}\n"
        with env_path.open("x", encoding="utf-8") as handle:
            handle.write(text)
        if os.name != "nt":
            env_path.chmod(0o600)
    (runtime / "source-path.txt").write_text(str(source.resolve()), encoding="utf-8")
    return source


def stack_command(runtime: Path, action: str) -> list[str]:
    source = Path((runtime / "source-path.txt").read_text(encoding="utf-8").strip())
    check_source(source)
    command = ["docker", "compose", "--project-name", "switch-incident-lab", "--env-file", str(runtime / "stack.env"),
               "--project-directory", str(source), "-f", str(source / "deploy/local/standalone-docker-compose.yml"),
               "-f", str(source / "deploy/local/standalone-docker-compose.build.yml"),
               "--profile", "collab", "--profile", "gateway"]
    return command + {"config": ["config", "--quiet"], "up": ["up", "-d", "--build"], "down": ["down"]}[action]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    local = sub.add_parser("replay", help="Run the deterministic, network-free exercise")
    local.add_argument("--scenario", type=Path, default=ROOT / "fixtures/scenario.json")
    local.add_argument("--context", action="append", default=[], help="Unverified human context to preserve")
    local.add_argument("--out", type=Path, default=ROOT / "reports")
    for name in ("plan", "provision"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--prefix", default="incident-lab")
        cmd.add_argument("--bridge", required=True, help="Exact local Mattermost bridge display name")
        if name == "provision":
            cmd.add_argument("--result", type=Path, default=ROOT / "reports/provision-result.json")
    check = sub.add_parser("validate-upstream")
    check.add_argument("--source", type=Path, required=True)
    init = sub.add_parser("prepare")
    init.add_argument("--runtime", type=Path, default=ROOT / ".runtime")
    init.add_argument("--source", type=Path)
    stack = sub.add_parser("stack")
    stack.add_argument("action", choices=["config", "up", "down"])
    stack.add_argument("--runtime", type=Path, default=ROOT / ".runtime")
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            report = replay(load_scenario(args.scenario), args.context)
            write_reports(report, args.out)
            print(json.dumps({"status": report["status"], "root_cause": report["root_cause"],
                              "output": str(args.out.resolve())}, indent=2))
        elif args.command in {"plan", "provision"}:
            inputs = incident_inputs(args.prefix, args.bridge)
            template = (ROOT / "templates/incident-rooms.template.yaml").read_text(encoding="utf-8")
            if args.command == "plan":
                print(json.dumps({"operation": "create_room_from_yaml", "arguments": {"yaml": template, "inputs": inputs}}, indent=2))
            else:
                if args.result.exists():
                    raise ValueError("A previous provisioning result exists; inspect rooms before starting another run with a new prefix")
                client = SwitchClient(os.environ.get("SWITCH_URL", "http://127.0.0.1:18000"),
                                      os.environ.get("SWITCH_AGENT_ID", ""), os.environ.get("SWITCH_AGENT_API_KEY", ""))
                result = client.provision(template, inputs)
                args.result.parent.mkdir(parents=True, exist_ok=True)
                args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print("Created two local incident rooms; result saved to " + str(args.result.resolve()))
        elif args.command == "validate-upstream":
            print(json.dumps(validate_upstream(args.source.resolve()), indent=2))
        elif args.command == "prepare":
            source = prepare(args.runtime.resolve(), args.source.resolve() if args.source else None)
            print("Pinned source ready: " + str(source))
            print("Generated credentials remain in the local runtime directory; none were printed.")
        elif args.command == "stack":
            subprocess.run(stack_command(args.runtime.resolve(), args.action), check=True)
            print("Local Compose " + args.action + " completed.")
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
