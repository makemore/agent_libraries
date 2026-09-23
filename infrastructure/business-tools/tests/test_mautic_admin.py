"""Synthetic Mautic 6.0.9 web-flow tests; no live services or credentials.

Run with root .venv/bin/python -B -m unittest discover
  -s infrastructure/business-tools/tests -p test_mautic_admin.py -v
"""

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from html import escape
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("business_tools_mautic_admin", ROOT / "scripts/mautic_admin.py")
mautic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mautic)
ORIGIN = "https://marketing.example.invalid"
CREDENTIALS = {"email": "operator@example.invalid", "username": "fixture-admin",
               "password": "synthetic-admin-password", "first_name": "Test", "last_name": "Operator"}
DB_PASSWORD = "synthetic-database-password"
TOKEN = "synthetic-token<&\""


def response(body="", status=200, location=None):
    headers = Message()
    if location is not None:
        headers["Location"] = location
    return SimpleNamespace(status=status, headers=headers, body=body.encode("utf-8"))


def redirect(path):
    return response(status=302, location=path)


def input_field(name, value="", kind="text", extra=""):
    return (f'<input name="{escape(name, quote=True)}" type="{kind}" '
            f'value="{escape(value, quote=True)}" {extra}>')


def form(name, action, fields):
    return f'<form name="{name}" method="post" action="{action}">{fields}</form>'


def installer_form(name, action, fields):
    fields += input_field(f"{name}[_token]", TOKEN + name, "hidden")
    fields += f'<button type="submit" name="{name}[buttons][next]">Next</button>'
    return response(form(name, action, fields))


def check_form():
    fields = "".join(input_field(f"{mautic.CHECK}[{key}]", value, "hidden")
                     for key, value in (("site_url", ORIGIN), ("cache_path", "%kernel.project_dir%/var/cache"),
                                        ("log_path", "%kernel.project_dir%/var/logs")))
    return installer_form(mautic.CHECK, "/installer/step/0", fields)


def database_form():
    name = mautic.DATABASE
    fields = (f'<select name="{name}[driver]"><option value="pdo_mysql">MySQL PDO</option></select>')
    fields += "".join(input_field(f"{name}[{key}]", value)
                      for key, value in (("host", "localhost"), ("port", "3306"), ("name", ""),
                                         ("table_prefix", ""), ("user", ""), ("backup_prefix", "bak_")))
    fields += input_field(f"{name}[password]", kind="password")
    fields += input_field(f"{name}[backup_tables]", "0", "radio")
    fields += input_field(f"{name}[backup_tables]", "1", "radio", "checked")
    return installer_form(name, "/installer/step/1", fields)


def admin_form():
    fields = "".join(input_field(f"{mautic.ADMIN}[{key}]", kind="password" if key == "password" else "text")
                     for key in ("firstname", "lastname", "email", "username", "password"))
    return installer_form(mautic.ADMIN, "/installer/step/2", fields)


def login_form():
    fields = input_field("_username") + input_field("_password", kind="password")
    fields += input_field("_csrf_token", TOKEN + "login", "hidden")
    fields += '<input name="_remember_me" type="checkbox"><button type="submit">Login</button>'
    return response(form("login", "/s/login_check", fields))


def account_form():
    fields = input_field("user[username]", CREDENTIALS["username"])
    fields += input_field("user[email]", CREDENTIALS["email"])
    return response(form("user", "/s/account", fields))


def installation():
    return [
        ("GET", "/installer", check_form()),
        ("POST", "/installer/step/0", redirect("/installer/step/1")),
        ("GET", "/installer/step/1", database_form()),
        ("POST", "/installer/step/1", redirect("/installer/step/1.1")),
        ("GET", "/installer/step/1.1", redirect("/installer/step/1.2")),
        ("GET", "/installer/step/1.2", redirect("/installer/step/2")),
        ("GET", "/installer/step/2", admin_form()),
        ("POST", "/installer/step/2", redirect("/installer/final")),
        ("GET", "/installer/final", response("Installation complete")),
        ("GET", "/installer", redirect(mautic.DASHBOARD)),
        ("GET", "/s/login", login_form()),
        ("POST", "/s/login_check", redirect(mautic.DASHBOARD)),
        ("GET", "/s/account", account_form()),
    ]


class Client:
    host = "marketing.example.invalid"
    origin = ORIGIN

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def request(self, method, path, data=None, form=None, headers=None):
        self.calls.append((method, path, form, headers))
        expected_method, expected_path, result = self.steps.pop(0)
        if (method, path) != (expected_method, expected_path) or data is not None:
            raise AssertionError("Unexpected synthetic request")
        if isinstance(result, Exception):
            raise result
        return result


class InitializationTests(unittest.TestCase):
    def setUp(self):
        # Fail closed if a future edit accidentally introduces networking.
        self.enterContext(mock.patch("socket.create_connection", side_effect=AssertionError("No network")))

    def apply(self, client, **kwargs):
        return mautic.initialize(client, CREDENTIALS, DB_PASSWORD, apply=True,
                                 fresh_install_verified=kwargs.get("fresh_install_verified", True))

    def assert_refused(self, callback):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(RuntimeError) as caught:
                callback()
        self.assertEqual(str(caught.exception), mautic.ERROR)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_apply_requires_literal_independent_true_before_any_request(self):
        for proof in (False, None, 0, 1, "true", [], {}):
            with self.subTest(proof_type=type(proof).__name__):
                client = Client([])
                self.assert_refused(lambda: self.apply(client, fresh_install_verified=proof))
                self.assertEqual(client.calls, [])
        client = Client([])
        self.assert_refused(lambda: mautic.initialize(client, CREDENTIALS, DB_PASSWORD, apply=True))
        self.assertEqual(client.calls, [])

    def test_preview_needs_no_secrets_or_proof_and_only_reads_installer(self):
        client = Client([("GET", "/installer", check_form())])
        result = mautic.initialize(client, None, None)
        self.assertEqual(result, {"preview": True, "initialized": False, "installer_available": True,
                                  "installer_locked": False, "login_verified": False})
        self.assertEqual([(call[0], call[1]) for call in client.calls], [("GET", "/installer")])

    def test_preview_never_follows_mutating_or_unknown_redirects_even_with_proof(self):
        for path in ("/installer/step/1.1", "/installer/step/1.2", "/installer/final", "/s/users/delete/1"):
            client = Client([("GET", "/installer", redirect(path))])
            self.assert_refused(lambda: mautic.initialize(client, None, None, fresh_install_verified=True))
            self.assertEqual(len(client.calls), 1)

    def test_installed_preview_reports_lockout_and_apply_never_reinstalls(self):
        client = Client([("GET", "/installer", redirect(ORIGIN + mautic.DASHBOARD))])
        result = mautic.initialize(client, None, None)
        self.assertTrue(result["installer_locked"])
        self.assertFalse(result["initialized"])
        self.assertEqual(len(client.calls), 1)
        client = Client([("GET", "/installer", redirect(mautic.DASHBOARD))])
        self.assert_refused(lambda: self.apply(client))
        self.assertEqual(len(client.calls), 1)

    def test_complete_supported_flow_preserves_csrf_defaults_and_checks_login(self):
        client = Client(installation())
        result = self.apply(client)
        self.assertEqual(client.steps, [])
        self.assertEqual(result, {"preview": False, "initialized": True, "installer_available": False,
                                  "installer_locked": True, "login_verified": True})
        self.assertTrue(all(type(value) is bool for value in result.values()))
        posts = {path: (fields, headers) for method, path, fields, headers in client.calls if method == "POST"}
        for path, name in (("/installer/step/0", mautic.CHECK), ("/installer/step/1", mautic.DATABASE),
                           ("/installer/step/2", mautic.ADMIN)):
            self.assertEqual(posts[path][0][f"{name}[_token]"], TOKEN + name)
            self.assertEqual(posts[path][1]["Origin"], ORIGIN)
        db = posts["/installer/step/1"][0]
        for key, value in (("host", "mautic-mariadb"), ("name", "mautic"), ("user", "mautic"),
                           ("password", DB_PASSWORD), ("port", "3306"), ("table_prefix", ""),
                           ("backup_tables", "1"), ("backup_prefix", "bak_")):
            self.assertEqual(db[f"{mautic.DATABASE}[{key}]"], value)
        admin = posts["/installer/step/2"][0]
        self.assertEqual(admin[f"{mautic.ADMIN}[firstname]"], CREDENTIALS["first_name"])
        self.assertEqual(admin[f"{mautic.ADMIN}[lastname]"], CREDENTIALS["last_name"])
        self.assertEqual(posts["/s/login_check"][0], {"_username": CREDENTIALS["username"],
                         "_password": CREDENTIALS["password"], "_csrf_token": TOKEN + "login"})
        self.assertNotIn("smtp", repr(client.calls).lower())

    def test_rendered_optional_values_are_preserved_not_replaced(self):
        # Named exception: emulate deliberate rendered DB prefix choices;
        # this parser test does not change a general fixture or deployment default.
        steps = installation()
        rendered = steps[2][2]
        rendered.body = rendered.body.replace(b'value="bak_"', b'value="chosen_backup_"')
        rendered.body = rendered.body.replace(b'[table_prefix]" type="text" value=""',
                                               b'[table_prefix]" type="text" value="chosen_"')
        client = Client(steps)
        self.apply(client)
        fields = client.calls[3][2]
        self.assertEqual(fields[f"{mautic.DATABASE}[backup_prefix]"], "chosen_backup_")
        self.assertEqual(fields[f"{mautic.DATABASE}[table_prefix]"], "chosen_")

    def test_alternate_database_port_is_refused_without_overwriting_it(self):
        # The caller's proof is for this stack's default MariaDB endpoint, not
        # another database a changed rendered port might address.
        steps = installation()[:3]
        item = steps[2][2]
        item.body = item.body.replace(b'value="3306"', b'value="3307"')
        before = item.body
        client = Client(steps)
        self.assert_refused(lambda: self.apply(client))
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(item.body, before)

    def test_invalid_origin_or_credentials_refused_before_requests(self):
        for origin in ("http://marketing.example.invalid", ORIGIN + "/", ORIGIN + ":443",
                       "https://other.example.invalid", ORIGIN + "?x=1"):
            client = Client([])
            client.origin = origin
            self.assert_refused(lambda: self.apply(client))
            self.assertEqual(client.calls, [])
        for key in CREDENTIALS:
            credentials = dict(CREDENTIALS)
            credentials[key] = ""
            client = Client([])
            self.assert_refused(lambda: mautic.initialize(client, credentials, DB_PASSWORD,
                                                          apply=True, fresh_install_verified=True))
            self.assertEqual(client.calls, [])

    def test_preview_does_not_accept_truthy_apply_flag(self):
        client = Client([])
        self.assert_refused(lambda: mautic.initialize(client, CREDENTIALS, DB_PASSWORD,
                                                      apply="false", fresh_install_verified=True))
        self.assertEqual(client.calls, [])

    def test_same_origin_absolute_and_relative_expected_redirects(self):
        steps = installation()
        steps[1] = ("POST", "/installer/step/0", redirect("1"))
        steps[3] = ("POST", "/installer/step/1", redirect(ORIGIN + "/installer/step/1.1"))
        client = Client(steps)
        self.assertTrue(self.apply(client)["initialized"])
        self.assertEqual(client.steps, [])

    def test_redirect_guard_rejects_external_credentials_ports_queries_and_other_paths(self):
        for target in ("https://other.example.invalid/installer/step/1.1", "//other.example.invalid/path",
                       "http://marketing.example.invalid/installer/step/1.1",
                       "https://user@marketing.example.invalid/installer/step/1.1",
                       ORIGIN + ":444/installer/step/1.1", "/installer/step/1.1?reset=1",
                       "/installer/step/1.1#fragment", "/installer/step/%31.1",
                       "/installer/step/1.1\n", "/\\other.example.invalid/path", "/installer/final"):
            steps = installation()[:4]
            steps[3] = ("POST", "/installer/step/1", redirect(target))
            client = Client(steps)
            self.assert_refused(lambda: self.apply(client))
            self.assertEqual(len(client.calls), 4)

    def test_no_post_replay_or_duplicate_location(self):
        for status in (307, 308):
            client = Client(installation()[:1] + [
                ("POST", "/installer/step/0", response(status=status, location="/installer/step/1"))])
            self.assert_refused(lambda: self.apply(client))
            self.assertEqual(len(client.calls), 2)
        duplicate = redirect(mautic.DASHBOARD)
        duplicate.headers["Location"] = mautic.DASHBOARD
        client = Client([("GET", "/installer", duplicate)])
        self.assert_refused(lambda: mautic.initialize(client, None, None))

    def test_bad_form_action_csrf_site_url_or_duplicate_form_stops_before_post(self):
        original = check_form().body
        invalid = [original.replace(b'/installer/step/0', b'https://other.example.invalid/steal'),
                   original.replace(b'[_token]', b'[unknown]'), original + original,
                   original.replace(ORIGIN.encode(), b'http://marketing.example.invalid'),
                   original.replace(b'method="post"', b'method="get"'),
                   b'<base href="https://other.example.invalid">' + original]
        for body in invalid:
            item = response()
            item.body = body
            client = Client([("GET", "/installer", item)])
            self.assert_refused(lambda: self.apply(client))
            self.assertEqual(len(client.calls), 1)

    def test_destructive_database_choice_is_refused_not_overridden(self):
        steps = installation()[:3]
        item = steps[2][2]
        item.body = item.body.replace(b'value="0" >', b'value="0" checked>')
        item.body = item.body.replace(b'value="1" checked>', b'value="1" >')
        client = Client(steps)
        self.assert_refused(lambda: self.apply(client))
        self.assertEqual(len(client.calls), 3)

    def test_failure_at_every_step_is_fixed_safe_and_never_retried(self):
        for index in range(len(installation())):
            for failure in (response("synthetic-private-response", status=500),
                            OSError("synthetic-private-transport-error")):
                steps = installation()[:index + 1]
                method, path, _ = steps[index]
                steps[index] = (method, path, failure)
                client = Client(steps)
                self.assert_refused(lambda: self.apply(client))
                self.assertEqual(len(client.calls), index + 1)

    def test_partial_install_form_error_does_not_retry_or_skip_ahead(self):
        for index, item in ((3, database_form()), (4, redirect("/installer/step/1")),
                            (5, redirect("/installer/step/1")), (7, admin_form())):
            steps = installation()[:index + 1]
            method, path, _ = steps[index]
            steps[index] = (method, path, item)
            client = Client(steps)
            self.assert_refused(lambda: self.apply(client))
            self.assertEqual(len(client.calls), index + 1)

    def test_installer_lockout_is_required_before_login(self):
        steps = installation()[:10]
        steps[9] = ("GET", "/installer", check_form())
        client = Client(steps)
        self.assert_refused(lambda: self.apply(client))
        self.assertEqual(len(client.calls), 10)

    def test_login_failure_and_wrong_account_are_not_success(self):
        steps = installation()[:12]
        steps[11] = ("POST", "/s/login_check", redirect("/s/login"))
        client = Client(steps)
        self.assert_refused(lambda: self.apply(client))
        self.assertEqual(len(client.calls), 12)
        for item in (login_form(), response("Arbitrary success page"), account_form()):
            item.body = item.body.replace(CREDENTIALS["email"].encode(), b"wrong@example.invalid")
            steps = installation()
            steps[12] = ("GET", "/s/account", item)
            client = Client(steps)
            self.assert_refused(lambda: self.apply(client))


class LiveMarkupRegressionTests(unittest.TestCase):
    def test_duplicate_autocomplete_hint_does_not_change_submitted_state(self):
        parser = mautic._Form('database')
        parser.feed('<form name="database"><input name="password" type="password" '
                    'autocomplete="off" autocomplete="new-password"></form>')
        self.assertEqual(parser.values, {'password': ''})

    def test_duplicate_submission_attribute_remains_refused(self):
        parser = mautic._Form('database')
        with self.assertRaises(RuntimeError):
            parser.feed('<form name="database"><input name="password" name="other"></form>')


if __name__ == "__main__":
    unittest.main()