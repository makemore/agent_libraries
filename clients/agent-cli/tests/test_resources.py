"""Isolated stdlib tests: fake transport only, no servers or real credentials."""

# Callbacks are evaluated immediately within each subtest, never deferred.
# ruff: noqa: B023

import copy
import json
import unittest
from urllib.parse import parse_qsl, urljoin, urlsplit

from agentctl.errors import CLIError
from agentctl.resources import ReadAPI

RUNTIME = "/host/runtime/"
STUDIO = "/host/studio/api/"
RUNS = RUNTIME + "runs/"
AGENTS = RUNTIME + "agents/"
PROJECTS = STUDIO + "workspace/projects/"
THREADS = STUDIO + "workspace/threads/"
# Synthetic marker used only to exercise exact-credential redaction.
MARKER = "fixture-credential-marker"


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.next_calls = []

    def get(self, path, query=None):
        self.calls.append((path, copy.deepcopy(query)))
        if not self.responses:
            raise AssertionError("Unexpected extra GET")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def next_query(self, url, path):
        self.next_calls.append((url, path))
        target = urlsplit(urljoin("https://api.example.test" + path, url))
        if (target.scheme != "https" or target.netloc != "api.example.test"
                or target.path != path or target.fragment):
            raise CLIError("Unsafe pagination destination.")
        pairs = parse_qsl(target.query, keep_blank_values=True)
        if len(dict(pairs)) != len(pairs):
            raise CLIError("Duplicate pagination options.")
        return dict(pairs)

    def redact(self, text):
        return text.replace(MARKER, "[REDACTED]")


def runtime_page(ids, *, count=None, next_url=None):
    return {"results": [{"id": value} for value in ids],
            "count": len(ids) if count is None else count, "next": next_url}


def thread_page(ids, *, count=None, more=False):
    return {"results": [{"id": value} for value in ids],
            "count": len(ids) if count is None else count, "has_more": more}


class ResourcesTests(unittest.TestCase):
    def api(self, *responses, **kwargs):
        client = FakeClient(*responses)
        return ReadAPI(client, runtime_mount=RUNTIME, studio_mount=STUDIO, **kwargs), client

    def assert_safe_error(self, call, *, exit_code=None):
        with self.assertRaises(CLIError) as caught:
            call()
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))
        if exit_code is not None:
            self.assertEqual(caught.exception.exit_code, exit_code)
        return caught.exception

    def test_runtime_agents_camel_case_projection_and_flat_envelope(self):
        api, client = self.api([{
            "id": 7, "slug": "helper", "name": MARKER + "\x1b[31m\n\u202eName",
            "isActive": True, "activeVersion": "1.2", "description": MARKER,
            "icon": {"sensitive": MARKER}, "versions": [{"system_prompt": MARKER}],
        }])
        result = api.list("runtime", "agents")
        self.assertEqual(set(result), {"items", "count", "has_more", "pages"})
        self.assertEqual((result["count"], result["has_more"], result["pages"]), (1, False, 1))
        row = result["items"][0]
        self.assertEqual(set(row), {"id", "slug", "name", "is_active", "active_version"})
        self.assertEqual(row["active_version"], "1.2")
        self.assertTrue(row["is_active"])
        self.assertTrue(row["name"].isprintable())
        self.assertNotIn(MARKER, json.dumps(result))
        self.assertEqual(client.calls, [(AGENTS, None)])

    def test_runs_include_only_allowlisted_metadata(self):
        expected = {
            "id": "run", "conversation_id": None, "agent_key": "helper", "status": "queued",
            "attempt": 0, "max_attempts": 3, "created_at": "2026-09-17", "started_at": None,
            "finished_at": None, "superseded_by_id": None, "cancel_requested_at": None,
            "suspended_at": None,
        }
        wire = dict(expected)
        wire["agentKey"] = wire.pop("agent_key")
        wire["maxAttempts"] = wire.pop("max_attempts")
        wire.update({key: {"secret": MARKER} for key in (
            "input", "output", "error", "metadata", "suspend_reason", "suspendReason", "idempotency_key",
        )})
        api, _ = self.api([wire])
        self.assertEqual(api.list("runtime", "runs")["items"], [expected])

    def test_studio_projections_are_snake_case_only(self):
        project = {"id": "org:one.project", "name": "One", "organization_id": "org",
                   "organization_name": "Org", "can_manage": True, "can_create_thread": False,
                   "revision": 2, "archived": False}
        thread = {"id": "thread", "project_id": "org:one.project", "title": "Title",
                  "agent_key": "helper", "conversation_id": "conversation", "visibility": "private",
                  "archived": False, "created_at": "today", "updated_at": "today",
                  "can_write": True, "can_manage": True}
        api, client = self.api(
            [{**project, "target": {"credentials": MARKER}, "organizationName": MARKER}],
            {"results": [{**thread, "shared_user_ids": [MARKER], "canWrite": MARKER}],
             "count": 1, "has_more": False},
        )
        self.assertEqual(api.list("studio", "projects")["items"], [project])
        self.assertEqual(api.list("studio", "threads")["items"], [thread])
        self.assertEqual(client.calls, [(PROJECTS, None), (THREADS, None)])

    def test_every_output_string_is_redacted_and_printable(self):
        for group, resource, row in (
            ("runtime", "agents", {"id": MARKER, "slug": MARKER, "name": MARKER,
                                   "activeVersion": MARKER}),
            ("runtime", "runs", {"id": MARKER, "agentKey": MARKER, "status": MARKER,
                                 "conversationId": MARKER, "createdAt": MARKER}),
            ("studio", "projects", {"id": MARKER, "name": MARKER, "organization_id": MARKER,
                                    "organization_name": MARKER}),
            ("studio", "threads", {"id": MARKER, "title": MARKER + "\r\x00\x9b\u2066\ud800",
                                   "project_id": MARKER, "conversation_id": MARKER}),
        ):
            with self.subTest(group=group, resource=resource):
                payload = {"results": [row], "count": 1, "has_more": False} if resource == "threads" else [row]
                api, _ = self.api(payload)
                result = api.list(group, resource)
                self.assertNotIn(MARKER, json.dumps(result))
                self.assertTrue(all(value.isprintable() for value in result["items"][0].values()))

    def test_allowed_gets_and_encoded_workspace_identifier(self):
        api, client = self.api({"id": "a", "versions": [MARKER]},
                               {"id": "p", "target": MARKER},
                               {"id": "t", "shared_user_ids": [MARKER]})
        self.assertEqual(api.get("runtime", "agents", "helper-1_2"), {"id": "a"})
        self.assertEqual(api.get("studio", "projects", "org:one.project"), {"id": "p"})
        self.assertEqual(api.get("studio", "threads", "a5bc237a-a5f0-4da8-8b51-cc188c370ff0"), {"id": "t"})
        self.assertEqual([path for path, _ in client.calls], [
            AGENTS + "helper-1_2/", PROJECTS + "org%3Aone.project/",
            THREADS + "a5bc237a-a5f0-4da8-8b51-cc188c370ff0/",
        ])

    def test_no_run_get_or_other_resource_operations(self):
        api, client = self.api()
        for group, resource in (("runtime", "runs"), ("runtime", "conversations"),
                                ("studio", "agents"), ("sdlc", "projects")):
            self.assert_safe_error(lambda: api.get(group, resource, "any"), exit_code=4)
        self.assert_safe_error(lambda: api.list("sdlc", "projects"), exit_code=4)
        self.assertEqual(client.calls, [])

    def test_missing_configuration_is_unsupported_without_requests(self):
        client = FakeClient()
        api = ReadAPI(client)
        for call in (lambda: api.list("runtime", "agents"), lambda: api.list("studio", "projects"),
                     lambda: api.get("runtime", "agents", "helper"), api.whoami, api.status):
            self.assert_safe_error(call, exit_code=4)
        self.assertEqual(client.calls, [])

    def test_unsafe_identifiers_and_paths_fail_before_get(self):
        for identifier in ("", ".", "..", "../other", "a/b", "a\\b", "%2e%2e", "a?x=1",
                           "a#fragment", "a\n", "é", "a" * 256, 42, None):
            for group, resource in (("runtime", "agents"), ("studio", "projects")):
                with self.subTest(identifier=identifier, group=group):
                    api, client = self.api()
                    self.assert_safe_error(lambda: api.get(group, resource, identifier))
                    self.assertEqual(client.calls, [])
        api, client = self.api()
        self.assert_safe_error(lambda: api.get("runtime", "agents", "a.b"))
        for path in ("", "relative/", "//other/", "/a/../b/", "/a/./b/", "/a/%2f/",
                     "/a?x=1", "/a#fragment", "https://example.test/a", "/a\n"):
            for option in ("runtime_mount", "studio_mount", "identity_path"):
                self.assert_safe_error(lambda: ReadAPI(client, **{option: path}))
        self.assertEqual(client.calls, [])

    def test_default_runtime_page_validates_next_without_following(self):
        next_url = "https://api.example.test" + AGENTS + "?page=2&system=tools"
        api, client = self.api(runtime_page(["a"], count=2, next_url=next_url))
        result = api.list("runtime", "agents", query={"system": "tools"})
        self.assertEqual(result, {"items": [{"id": "a"}], "count": 2, "has_more": True, "pages": 1})
        self.assertEqual(client.calls, [(AGENTS, {"system": "tools"})])
        self.assertEqual(client.next_calls, [(next_url, AGENTS)])
        self.assertNotIn("next", result)

    def test_runtime_all_pages_keeps_filters_and_uses_only_list_path(self):
        query = {"agent_key": "dynamic:helper", "page_size": "1"}
        api, client = self.api(
            runtime_page(["a"], count=2, next_url="?page=2&page_size=1&agent_key=dynamic%3Ahelper"),
            runtime_page(["b"], count=2),
        )
        result = api.list("runtime", "runs", query=query, all_pages=True)
        self.assertEqual(result, {"items": [{"id": "a"}, {"id": "b"}], "count": 2,
                                  "has_more": False, "pages": 2})
        self.assertEqual(client.calls, [(RUNS, {"agent_key": "dynamic:helper", "page_size": 1}),
                                        (RUNS, {"agent_key": "dynamic:helper", "page_size": 1, "page": 2})])
        self.assertEqual(query, {"agent_key": "dynamic:helper", "page_size": "1"})

    def test_runtime_terminal_count_can_include_omitted_expired_runs(self):
        api, _ = self.api(runtime_page(["a"], count=4))
        self.assertEqual(api.list("runtime", "runs", all_pages=True)["count"], 4)

    def test_runtime_retention_can_omit_whole_nonterminal_pages(self):
        api, client = self.api(runtime_page([], count=3, next_url="?page=2"),
                               runtime_page(["visible"], count=3))
        result = api.list("runtime", "runs", all_pages=True)
        self.assertEqual(result["items"], [{"id": "visible"}])
        self.assertEqual(len(client.calls), 2)

    def test_empty_lists_and_completion_at_page_cap_succeed(self):
        for group, resource, payload in (
            ("runtime", "agents", []), ("runtime", "runs", runtime_page([])),
            ("studio", "projects", []), ("studio", "threads", thread_page([])),
        ):
            api, client = self.api(payload)
            self.assertEqual(api.list(group, resource, all_pages=True, max_pages=1),
                             {"items": [], "count": 0, "has_more": False, "pages": 1})
            self.assertEqual(len(client.calls), 1)
        api, client = self.api(runtime_page(["a"], count=3, next_url="?page=3"),
                               runtime_page(["b"], count=3))
        result = api.list("runtime", "agents", query={"page": "2"}, all_pages=True, max_pages=2)
        self.assertEqual((result["pages"], result["has_more"]), (2, False))
        self.assertEqual(client.calls, [(AGENTS, {"page": 2}), (AGENTS, {"page": 3})])

    def test_threads_offset_uses_returned_count_and_preserves_filters(self):
        query = {"project_id": "org:project", "archived": False, "limit": "3", "offset": "1"}
        api, client = self.api(thread_page(["a"], count=3, more=True), thread_page(["b"], count=3))
        result = api.list("studio", "threads", query=query, all_pages=True)
        self.assertEqual((result["pages"], result["has_more"], result["count"]), (2, False, 3))
        self.assertEqual(client.calls, [
            (THREADS, {"project_id": "org:project", "archived": "false", "limit": 3, "offset": 1}),
            (THREADS, {"project_id": "org:project", "archived": "false", "limit": 3, "offset": 2}),
        ])
        self.assertEqual(query["offset"], "1")
        self.assertEqual(client.next_calls, [])

    def test_threads_default_single_page_and_offset_beyond_count(self):
        api, client = self.api(thread_page(["a"], count=2, more=True))
        self.assertTrue(api.list("studio", "threads")["has_more"])
        self.assertEqual(len(client.calls), 1)
        api, _ = self.api(thread_page([], count=2))
        self.assertEqual(api.list("studio", "threads", query={"offset": 10})["items"], [])

    def test_unsafe_next_urls_never_make_another_request(self):
        for url in ("https://other.example.test" + RUNS + "?page=2", "//other.example.test/runs/?page=2",
                    AGENTS + "?page=2", RUNS + "run-id/?page=2", RUNS + "by-idempotency-key/?page=2",
                    RUNS + "../agents/?page=2", RUNS + "%2e%2e/agents/?page=2",
                    RUNS + "?page=2#" + MARKER, "?page=2&page=3"):
            with self.subTest(url=url):
                api, client = self.api(runtime_page(["a"], count=3, next_url=url))
                self.assert_safe_error(lambda: api.list("runtime", "runs", all_pages=True))
                self.assertEqual(len(client.calls), 1)

    def test_nonprogressing_or_changed_next_queries_fail_closed(self):
        for url in ("?page=1", "?page=3", "?page=2&format=html", "?page=2&agent_key=other",
                    "?page=2&page_size=100", "?page=0", "?page=last"):
            with self.subTest(url=url):
                api, client = self.api(runtime_page(["a"], count=3, next_url=url))
                self.assert_safe_error(lambda: api.list("runtime", "runs", all_pages=True))
                self.assertEqual(len(client.calls), 1)
        api, client = self.api(runtime_page(["a"], count=3, next_url="?page=2"))
        self.assert_safe_error(lambda: api.list("runtime", "runs", query={"agent_key": "helper"}))
        self.assertEqual(len(client.calls), 1)

    def test_cycles_repeated_rows_count_changes_and_envelope_changes(self):
        first = runtime_page(["a"], count=3, next_url="?page=2")
        for second in (runtime_page(["b"], count=3, next_url="?page=1"),
                       runtime_page(["a"], count=3), runtime_page(["b"], count=4), [{"id": "b"}]):
            api, client = self.api(first, second)
            self.assert_safe_error(lambda: api.list("runtime", "runs", all_pages=True))
            self.assertEqual(len(client.calls), 2)
        api, _ = self.api([{"id": "a"}, {"id": "a"}])
        self.assert_safe_error(lambda: api.list("runtime", "agents"))
        api, _ = self.api(thread_page(["a"], count=2, more=True), thread_page(["a"], count=2))
        self.assert_safe_error(lambda: api.list("studio", "threads", all_pages=True))

    def test_page_caps_fail_instead_of_returning_partial_success(self):
        for group, resource, payload in (
            ("runtime", "runs", runtime_page(["a"], count=2, next_url="?page=2")),
            ("studio", "threads", thread_page(["a"], count=2, more=True)),
        ):
            api, client = self.api(payload)
            error = self.assert_safe_error(lambda: api.list(group, resource, all_pages=True, max_pages=1))
            self.assertEqual(error.code, "pagination")
            self.assertEqual(len(client.calls), 1)
        pages = [runtime_page([str(i)], count=101, next_url=f"?page={i + 2}") for i in range(100)]
        api, client = self.api(*pages)
        self.assert_safe_error(lambda: api.list("runtime", "runs", all_pages=True, max_pages=100))
        self.assertEqual(len(client.calls), 100)
        api, client = self.api(*pages[:10])
        self.assert_safe_error(lambda: api.list("runtime", "runs", all_pages=True))
        self.assertEqual(len(client.calls), 10)

    def test_invalid_caps_and_queries_do_not_request(self):
        api, client = self.api()
        for cap in (0, -1, 101, True, 1.0, "10", None):
            self.assert_safe_error(lambda: api.list("runtime", "agents", max_pages=cap))
        self.assert_safe_error(lambda: api.list("runtime", "agents", all_pages="true"))
        for group, resource, query in (
            ("studio", "projects", {"page": 1}), ("studio", "threads", {"q": MARKER}),
            ("runtime", "agents", {"agent_key": "helper"}), ("runtime", "runs", {"status": MARKER}),
            ("runtime", "runs", {"agent_key": {"secret": MARKER}}),
            ("runtime", "agents", {"system": "../other"}), ("runtime", "agents", {"page": True}),
            ("runtime", "agents", {"page_size": 0}), ("runtime", "agents", {"page": "9" * 5000}),
            ("studio", "threads", {"limit": 101}), ("studio", "threads", {"limit": 0}),
            ("studio", "threads", {"offset": -1}), ("studio", "threads", {"offset": 2147483648}),
            ("studio", "threads", {"archived": "yes"}), ("studio", "threads", {"archived": []}),
            ("studio", "threads", {"project_id": "../other"}), ("runtime", "runs", []),
        ):
            with self.subTest(group=group, resource=resource, query_type=type(query)):
                self.assert_safe_error(lambda: api.list(group, resource, query=query))
        self.assertEqual(client.calls, [])

    def test_malformed_envelopes_counts_and_arrays(self):
        malformed = [None, "<html>" + MARKER, {"detail": MARKER}, {"items": []},
                     {"results": {}, "count": 0, "next": None},
                     {"results": [], "count": -1, "next": None},
                     {"results": [], "count": True, "next": None},
                     {"results": [], "count": "0", "next": None},
                     {"results": [], "count": 0.0, "next": None},
                     {"results": [], "count": 0}, {"results": [], "count": 0, "next": ""},
                     {"results": [], "count": 0, "next": {"secret": MARKER}},
                     runtime_page(["a"], count=0),
                     runtime_page(["a"], count=1, next_url="?page=2")]
        for payload in malformed:
            api, _ = self.api(payload)
            self.assert_safe_error(lambda: api.list("runtime", "runs", all_pages=True))
        for payload in ([], {"results": [], "count": 0, "hasMore": False},
                        {"results": [], "count": 0, "has_more": "false"},
                        thread_page([], count=1, more=True), thread_page(["a"], count=2),
                        thread_page(["a"], count=1, more=True), thread_page(["a", "b"])):
            api, _ = self.api(payload)
            self.assert_safe_error(lambda: api.list("studio", "threads", query={"limit": 1}))
        api, _ = self.api(runtime_page([]))
        self.assert_safe_error(lambda: api.list("studio", "projects"))

    def test_malformed_rows_and_nested_metadata_fail_closed(self):
        for row in (None, MARKER, [], {}, {"detail": MARKER}, {"id": None}, {"id": True},
                    {"id": []}, {"id": ""}, {"id": "a", "name": {"secret": MARKER}},
                    {"id": "a", "activeVersion": [MARKER]}, {"id": "a", "name": float("nan")},
                    {"id": "a", "name": float("inf")},
                    {"id": "a", "active_version": "1", "activeVersion": "2"}):
            api, _ = self.api([row])
            self.assert_safe_error(lambda: api.list("runtime", "agents"))
        api, _ = self.api({"id": "a", "title": {"secret": MARKER}})
        self.assert_safe_error(lambda: api.get("studio", "threads", "thread"))

    def test_redacted_identifiers_do_not_cause_false_duplicate_detection(self):
        api, _ = self.api([{"id": MARKER}, {"id": "[REDACTED]"}])
        self.assertEqual(len(api.list("runtime", "agents")["items"]), 2)

    def test_identity_is_explicit_and_normalizes_pk(self):
        wire = {"pk": 42, "username": MARKER, "email": MARKER + "@example.test",
                "fullName": "Name\u202e", "preferred_name": "Short", "is_active": True,
                "groups": [MARKER], "permissions": {"all": True}, "token": MARKER}
        api, client = self.api(wire, identity_path="/accounts/current/")
        self.assertEqual(api.whoami(), {"id": 42, "username": "[REDACTED]",
                                       "email": "[REDACTED]@example.test", "full_name": "Name ",
                                       "preferred_name": "Short", "is_active": True})
        self.assertEqual(client.calls, [("/accounts/current/", None)])
        api, _ = self.api({"id": 1, "pk": 2}, identity_path="/me/")
        self.assertEqual(api.whoami(), {"id": 1})
        api, client = self.api({"id": None, "pk": MARKER}, identity_path="/me")
        self.assertEqual(api.whoami(), {"id": "[REDACTED]"})
        self.assertEqual(client.calls, [("/me", None)])
        for payload in ("<html>", {"detail": MARKER}, {"username": MARKER},
                        {"pk": {"secret": MARKER}}, {"id": 1, "email": [MARKER]}):
            api, _ = self.api(payload, identity_path="/me/")
            self.assert_safe_error(api.whoami)

    def test_status_is_bounded_observation_not_capability_or_auth_grant(self):
        api, client = self.api(
            runtime_page(["a"], count=2, next_url="?page=2&page_size=1"),
            CLIError(MARKER, code=MARKER, status=403), {"id": 1, "email": MARKER}, identity_path="/me/",
        )
        result = api.status()
        self.assertEqual(result["observations"], [
            {"group": "runtime", "resource": "agents", "availability": "observed_available"},
            {"group": "studio", "resource": "projects", "availability": "observed_unavailable", "status": 403},
            {"group": "identity", "resource": "whoami", "availability": "observed_available"},
        ])
        self.assertIn("not capability or authorization grants", result["scope"])
        self.assertNotIn(MARKER, json.dumps(result))
        self.assertEqual(client.calls, [(AGENTS, {"page_size": 1}), (PROJECTS, None), ("/me/", None)])
        client = FakeClient([])
        self.assertEqual(len(ReadAPI(client, studio_mount=STUDIO).status()["observations"]), 1)
        self.assertEqual(client.calls, [(PROJECTS, None)])


if __name__ == "__main__":
    unittest.main()
