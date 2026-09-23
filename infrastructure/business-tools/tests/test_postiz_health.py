"""Execute the rendered production probe in a Node HTTP/timer mock sandbox.

No sockets, containers, credentials or live services; Python uses only stdlib.
Node is optional and explicitly skipped when unavailable. The virtual clock tests
the production deadlines unchanged, without spending eight seconds per failure.
Run with root .venv/bin/python -m unittest discover
  -s infrastructure/business-tools/tests -p test_postiz_health.py -v
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "agentic-social/compose.yaml.tftpl"
API = "/api/auth/can-register"
NODE = shutil.which("node")


def rendered_template():
    # This template has only public images/domains substitutions, no directives.
    # Compose schema and real Terraform rendering remain covered by existing tests.
    rendered = re.sub(
        r"\$\{(images|domains)\.([a-z_]+)\}",
        lambda m: (f"example.invalid/{m[2]}:fixture" if m[1] == "images"
                   else f"{m[2]}.example.invalid"),
        TEMPLATE.read_text(),
    )
    if "${" in rendered or "%{" in rendered:
        raise AssertionError("Unsupported template expression")
    return rendered


def service(template, name):
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][\w-]*:|^networks:|\Z)",
        template, re.M | re.S,
    )
    if match is None:
        raise AssertionError("Missing service")
    return match[1]


def inline_probe(template):
    # Parse only this literal scalar; preserve every JS byte after YAML indentation.
    match = re.search(
        r"^      test:\n        - CMD\n        - node\n        - -e\n"
        r"        - \|\n((?:          [^\n]*\n)+)",
        service(template, "postiz"), re.M,
    )
    if match is None:
        raise AssertionError("Expected an inline Node literal healthcheck")
    return "\n".join(line[10:] for line in match[1].splitlines()) + "\n"


# The sandbox require() cannot load real http/net or access the filesystem. Only
# the harness reads synthetic stdin. Real process.exit preserves exit status and
# console is deliberately not suppressed, so accidental probe output fails tests.
NODE_SANDBOX = r"""
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let now = 0;
let sequence = 0;
const jobs = [];
const paths = [];
function schedule(fn, delay = 0) {
  const job = {fn, at: now + delay, order: sequence++, cancelled: false};
  jobs.push(job);
  return job;
}
function cancel(job) { if (job) job.cancelled = true; }
const http = {
  get(address, callback) {
    const url = new URL(address);
    assert.equal(url.origin, 'http://127.0.0.1:5000');
    assert.ok(Object.hasOwn(input.responses, url.pathname));
    assert.equal(url.search, '');
    paths.push(url.pathname);
    const spec = input.responses[url.pathname];
    const request = new EventEmitter();
    let socketTimer;
    let socketDelay;
    let socketCallback;
    function activity() {
      cancel(socketTimer);
      if (socketCallback) socketTimer = schedule(socketCallback, socketDelay);
    }
    request.setTimeout = (delay, callback) => {
      assert.ok(delay > 0 && delay < 10000);
      socketDelay = delay;
      socketCallback = callback;
      // A connection that never opens may not start Node's socket idle timer.
      if (spec.mode !== 'connecting') activity();
      return request;
    };
    request.destroy = () => cancel(socketTimer);
    if (spec.mode === 'throw') throw new Error('synthetic get failure');
    schedule(() => {
      if (spec.mode === 'refused') {
        request.emit('error', new Error('synthetic ECONNREFUSED'));
        return;
      }
      if (spec.mode === 'hung' || spec.mode === 'connecting') return;
      const response = new EventEmitter();
      response.statusCode = spec.status;
      response.complete = false;
      activity();
      callback(response);
      if (spec.mode === 'response-error') {
        response.emit('error', new Error('synthetic response failure'));
        return;
      }
      if (spec.mode === 'aborted') { response.emit('aborted'); return; }
      if (spec.mode === 'early-close') { response.emit('close'); return; }
      if (spec.mode === 'stalled') return;
      if (spec.mode === 'trickle') {
        function drip() {
          activity();
          response.emit('data', Buffer.from(' '));
          schedule(drip, 1000);
        }
        drip();
        return;
      }
      const body = Buffer.from(spec.body);
      const chunkSize = spec.chunkSize || body.length || 1;
      let count = 0;
      for (let offset = 0; offset < body.length; offset += chunkSize) {
        const chunk = body.subarray(offset, offset + chunkSize);
        schedule(() => { activity(); response.emit('data', chunk); }, ++count);
      }
      schedule(() => {
        cancel(socketTimer);
        response.complete = spec.mode !== 'truncated';
        response.emit('end');
        response.emit('close');
      }, count + 1);
    });
    return request;
  }
};
const sandbox = {
  require(name) { assert.equal(name, 'http'); return http; },
  Buffer,
  console,
  process: {exit(code) {
    assert.ok(code === 0 || code === 1);
    assert.ok(now >= input.minExitMs && now <= input.maxExitMs);
    if (code === 0) assert.deepEqual(paths.sort(), ['/', '/api/auth/can-register']);
    process.exit(code);
  }},
  setTimeout(fn, delay) {
    assert.ok(delay > 0 && delay < 10000);
    return schedule(fn, delay);
  },
  clearTimeout: cancel
};
async function run() {
  vm.runInNewContext(input.probe, sandbox, {timeout: 1000});
  // Yield between virtual events so Promise callbacks run just as in Node's loop.
  await new Promise(resolve => setImmediate(resolve));
  for (let step = 0; jobs.length && step < 10000; step++) {
    jobs.sort((a, b) => a.at - b.at || a.order - b.order);
    const job = jobs.shift();
    if (job.cancelled) continue;
    now = job.at;
    job.fn();
    await new Promise(resolve => setImmediate(resolve));
  }
  process.exit(98); // A probe must explicitly succeed or fail, never fall through.
}
run().catch(() => process.exit(99));
"""


class PostizHealthContractTests(unittest.TestCase):
    def test_temporal_production_environment_skips_demo_attributes_only(self):
        rendered = rendered_template()
        temporal = service(rendered, "postiz-temporal")
        environment = temporal.split("    environment:\n", 1)[1].split("    networks:", 1)[0]
        settings = dict(re.findall(r"^      ([A-Z_][A-Z0-9_]*): (.+)$", environment, re.M))
        self.assertEqual(settings, {
            "DB": "postgres12", "DB_PORT": '"5432"', "POSTGRES_USER": "temporal",
            "POSTGRES_SEEDS": "postiz-temporal-postgres",
            "TEMPORAL_ADDRESS": '"postiz-temporal:7233"', "BIND_ON_IP": '"0.0.0.0"',
            "SKIP_ADD_CUSTOM_SEARCH_ATTRIBUTES": '"true"',
        })
        self.assertEqual(rendered.count('SKIP_ADD_CUSTOM_SEARCH_ATTRIBUTES: "true"'), 1)
        for field in ("volumes", "command", "entrypoint", "configs"):
            self.assertNotRegex(temporal, rf"(?m)^    {field}:")
        for override in ("DYNAMIC_CONFIG_FILE_PATH", "NUM_CUSTOM_SEARCH_ATTRIBUTES",
                         "SKIP_SCHEMA_SETUP", "SKIP_DEFAULT_NAMESPACE_CREATION",
                         "DEFAULT_NAMESPACE_RETENTION", "ENABLE_ES"):
            self.assertNotRegex(rendered, rf"(?m)^\s*{override}:")
        self.assertNotRegex(rendered, r"(?i)searchAttributesNumberOf|searchAttributes.*limit:")
        self.assertIn("operator namespace describe", temporal)
        self.assertIn("--namespace default", temporal)

    def test_probe_is_inline_and_preserves_health_and_auth_contracts(self):
        rendered = rendered_template()
        web = service(rendered, "postiz")
        probe = inline_probe(rendered)
        self.assertNotIn("$", probe)  # No Compose/template interpolation in JS.
        for field in ("interval: 30s", "timeout: 10s", "retries: 5", "start_period: 120s",
                      "stop_grace_period: 60s", 'DISABLE_REGISTRATION: "true"'):
            self.assertIn(field, web)
        self.assertEqual(web.count("target:"), 2)  # No mounted health asset.
        self.assertIn("check('/', false, 1048576)", probe)
        self.assertIn("check('/api/auth/can-register', true, 4096)", probe)

    def test_exception_reason_approval_and_removal_are_documented(self):
        readme = (ROOT / "agentic-social/README.md").read_text()
        for text in ("2026-09-20", "v2.23.0", "auto-setup:1.28.1", "CustomTextField",
                     "CustomStringField", "organizationId", "postId", "three Text",
                     "future demo-attribute creation only", "not remove existing attributes",
                     "Removal criterion", "Container healthy != application readiness",
                     "test_postiz_health.py"):
            self.assertIn(text, readme)


@unittest.skipUnless(NODE, "Node unavailable; static production contracts still run")
class PostizInlineProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = inline_probe(rendered_template())

    def run_probe(self, expected, *, frontend=None, api=None, min_ms=0, max_ms=100):
        payload = {
            "probe": self.probe, "minExitMs": min_ms, "maxExitMs": max_ms,
            "responses": {
                "/": {"status": 200, "body": "<html>frontend</html>", **(frontend or {})},
                API: {"status": 200, "body": '{"register":true}', **(api or {})},
            },
        }
        with tempfile.TemporaryDirectory(prefix="postiz-health-test-") as directory:
            result = subprocess.run(
                [NODE, "-e", NODE_SANDBOX], input=json.dumps(payload), cwd=directory,
                env={}, text=True, capture_output=True, timeout=5, check=False,
            )
        self.assertEqual(result.returncode, expected, "Unexpected inline probe exit status")
        self.assertEqual(result.stdout, "", "Probe must not print response bodies")
        self.assertEqual(result.stderr, "", "Probe must not print diagnostics")

    def test_boolean_true_and_false_are_healthy_with_frontend_success_or_redirect(self):
        for register in (True, False):
            for status in (200, 302, 307):
                with self.subTest(register=register, frontend_status=status):
                    self.run_probe(0, frontend={"status": status},
                                   api={"body": json.dumps({"register": register})})

    def test_healthy_frontend_does_not_hide_backend_502(self):
        self.run_probe(1, api={"status": 502, "body": "<html>Bad Gateway</html>"})

    def test_api_requires_exact_status_200_even_with_valid_boolean_json(self):
        for status in (201, 204, 301, 302, 307, 400, 401, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.run_probe(1, api={"status": status})

    def test_frontend_client_and_server_errors_fail_even_with_healthy_api(self):
        for status in (400, 401, 404, 500, 502, 503):
            with self.subTest(status=status):
                self.run_probe(1, frontend={"status": status})

    def test_api_rejects_malformed_json_and_non_boolean_register(self):
        bodies = ('', '<html>not JSON</html>', '{"register":', '{"register":true}junk',
                  '{}', 'null', 'true', '[]', '[{"register":true}]',
                  '{"register":"true"}', '{"register":"false"}', '{"register":1}',
                  '{"register":0}', '{"register":null}', '{"register":{}}')
        for body in bodies:
            with self.subTest(body=body):
                self.run_probe(1, api={"body": body})

    def test_chunked_json_is_collected_before_validation(self):
        self.run_probe(0, api={"body": '{"register":false}', "chunkSize": 1})

    def test_transport_and_incomplete_response_failures_are_quiet_on_either_route(self):
        for route in ("frontend", "api"):
            for mode in ("refused", "throw", "response-error", "aborted", "early-close", "truncated"):
                with self.subTest(route=route, mode=mode):
                    # Truncated API content is valid JSON: HTTP completeness is independent.
                    self.run_probe(1, **{route: {"mode": mode}})

    def test_connected_socket_and_response_stalls_hit_socket_timeout(self):
        for route in ("frontend", "api"):
            for mode in ("hung", "stalled"):
                with self.subTest(route=route, mode=mode):
                    self.run_probe(1, **{route: {"mode": mode}}, min_ms=3000, max_ms=3000)

    def test_unopened_connections_hit_overall_deadline_without_socket_timeout(self):
        for route in ("frontend", "api"):
            with self.subTest(route=route):
                self.run_probe(1, **{route: {"mode": "connecting"}}, min_ms=8000, max_ms=8000)

    def test_trickling_responses_cannot_extend_overall_deadline(self):
        for route in ("frontend", "api"):
            with self.subTest(route=route):
                self.run_probe(1, **{route: {"mode": "trickle"}}, min_ms=8000, max_ms=8000)

    def test_byte_limits_accept_boundary_and_reject_oversize_before_timeout(self):
        for route, limit, prefix in (("frontend", 1048576, "<html>"),
                                     ("api", 4096, '{"register":false}')):
            body = prefix + " " * (limit - len(prefix))
            with self.subTest(route=route):
                self.run_probe(0, **{route: {"body": body}})
                self.run_probe(1, **{route: {"body": body + " "}})
                self.run_probe(1, **{route: {"body": body + " ", "chunkSize": limit // 2}})
        # Count UTF-8 bytes, not JS string characters, even when the JSON is valid.
        self.run_probe(1, api={"body": json.dumps(
            {"register": True, "padding": "é" * 2500}, ensure_ascii=False)})


if __name__ == "__main__":
    unittest.main()