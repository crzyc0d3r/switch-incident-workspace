"""Exercise causal controls and the exact local HTTP transport contract."""

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

import yaml

import workspace


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.scenario = workspace.load_scenario(workspace.ROOT / "fixtures/scenario.json")

    def test_two_symptoms_and_config_only_rollback(self):
        report = workspace.replay(self.scenario)
        self.assertEqual(report["status"], "confirmed_in_simulation")
        self.assertEqual(report["current_probes"]["INC-101"]["http_status"], 500)
        training = report["current_probes"]["INC-102"]
        self.assertEqual((training["job_status"], training["completed_batches"], training["stalled_hours"]),
                         ("RUNNING", 0, 6))
        self.assertEqual(training["retry_attempts"], 360)
        self.assertEqual(report["config_rollback_probes"]["INC-101"]["http_status"], 200)
        self.assertEqual(report["config_rollback_probes"]["INC-102"]["completed_batches"], 1)
        self.assertEqual(report["old_image_control"]["http_status"], 500)
        self.assertEqual(report["old_image_control"]["image"], "infer:1.7.0")
        self.assertTrue(all(report["checks"].values()))

    def test_coincident_timestamps_do_not_override_different_revisions(self):
        self.scenario["incidents"][1]["config_revision"] = "cfg-183"
        report = workspace.replay(self.scenario)
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["checks"]["same_effective_revision"])
        self.assertEqual(report["current_probes"]["INC-102"]["completed_batches"], 1)

    def test_failed_rollback_prevents_confirmation(self):
        self.scenario["configs"]["previous"]["endpoint"] = "http://also-unavailable.invalid"
        report = workspace.replay(self.scenario)
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["checks"]["both_recover_on_config_rollback"])

    def test_mismatched_logs_prevent_confirmation(self):
        self.scenario["incidents"][1]["evidence"][0]["detail"] = "CUDA out of memory"
        report = workspace.replay(self.scenario)
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["checks"]["error_logs_match_probe_hosts"])

    def test_human_context_preserved_and_escaped_without_changing_checks(self):
        statement = '<script>alert("context")</script> Config cannot be the cause.'
        baseline = workspace.replay(self.scenario)
        report = workspace.replay(self.scenario, [statement])
        self.assertEqual(report["checks"], baseline["checks"])
        self.assertFalse(report["human_context"][0]["verified"])
        self.assertEqual(report["human_context"][0]["text"], statement)
        with tempfile.TemporaryDirectory() as directory:
            workspace.write_reports(report, Path(directory))
            page = (Path(directory) / "report.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", page)
            self.assertIn("&lt;script&gt;", page)
            restored = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(restored, report)

    def test_replay_is_deterministic_and_links_both_directions(self):
        original = copy.deepcopy(self.scenario)
        report = workspace.replay(self.scenario)
        self.assertEqual(report, workspace.replay(self.scenario))
        self.assertEqual(self.scenario, original)
        links = {(e["room"], e["target"]) for e in report["transcript"] if e["action"] == "link_room"}
        self.assertEqual(links, {("inference-500", "training-frozen"), ("training-frozen", "inference-500")})
        self.assertEqual(report["transcript"][-1]["role"], "Scenario Manager")

    def test_fixture_rejects_zero_retry_interval(self):
        self.scenario["incidents"][1]["retry_interval_seconds"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(self.scenario), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be positive"):
                workspace.load_scenario(path)


class TemplateTests(unittest.TestCase):
    def test_template_links_and_role_contract(self):
        doc = yaml.safe_load((workspace.ROOT / "templates/incident-rooms.template.yaml").read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], 2)
        resources = doc["resources"]
        self.assertEqual({(link["from"], link["to"]) for link in resources["incidents"]["links"]},
                         {("inference", "training"), ("training", "inference")})
        for name in ("inference", "training"):
            self.assertEqual({r["name"] for r in resources[name]["roles"]}, {"software-engineer", "architect", "qa"})
            self.assertIn("{$creator}", resources[name]["users"])
            self.assertEqual(len(resources[name]["agents"]), 4)

    def test_input_prefix_cannot_inject_a_resource_name(self):
        for prefix in ("UPPERCASE", "space here", "../outside", "bad\nname"):
            with self.assertRaises(ValueError):
                workspace.incident_inputs(prefix, "Local Mattermost")


class LocalContractTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.redirect = False
        self.schema = {"properties": {"yaml": {"type": "string"}, "inputs": {"type": "object"}}, "required": ["yaml"]}
        self.result = {"group_id": "g", "rooms": [{"room_id": "r1", "failed_attachments": []},
                                                   {"room_id": "r2", "failed_attachments": []}], "errors": []}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.calls.append(("GET", self.path, self.headers.get("Authorization"), None))
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-the-key")
                    self.end_headers()
                    return
                self.reply({"operations": {"create_room_from_yaml": {"input_schema": owner.schema}}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(("POST", self.path, self.headers.get("Authorization"), body))
                self.reply({"result": owner.result})

            def reply(self, data):
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = workspace.SwitchClient(self.url, "manager-id", "local-test-placeholder")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_discovery_then_unwrapped_arguments_and_bearer_header(self):
        inputs = workspace.incident_inputs("incident-lab", "Local Mattermost")
        result = self.client.provision("version: 2\nresources: {}", inputs)
        self.assertEqual(len(result["rooms"]), 2)
        self.assertEqual(self.calls[0][:2], ("GET", "/agents/manager-id/ops"))
        post = self.calls[1]
        self.assertEqual(post[:3], ("POST", "/agents/manager-id/ops/create_room_from_yaml", "Bearer local-test-placeholder"))
        self.assertEqual(set(post[3]), {"yaml", "inputs"})
        self.assertEqual(post[3]["inputs"], inputs)

    def test_schema_drift_stops_before_provisioning(self):
        self.schema["required"].append("unexpected_argument")
        with self.assertRaisesRegex(RuntimeError, "schema differs"):
            self.client.provision("ignored", {})
        self.assertEqual(len(self.calls), 1)

    def test_redirect_never_forwards_request_or_credential(self):
        self.redirect = True
        with self.assertRaisesRegex(RuntimeError, "HTTP 302") as caught:
            self.client.provision("ignored", {})
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("local-test-placeholder", str(caught.exception))

    def test_partial_provisioning_is_not_reported_as_success(self):
        self.result["rooms"][1]["failed_attachments"] = [{"kind": "document", "error": "failed"}]
        with self.assertRaisesRegex(RuntimeError, "partial"):
            self.client.provision("ignored", {})

    def test_remote_urls_rejected_before_any_request(self):
        for url in ("https://example.invalid", "http://127.0.0.1.example.invalid", "http://user:pass@localhost"):
            with self.assertRaises(ValueError):
                workspace.SwitchClient(url, "manager", "placeholder")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
