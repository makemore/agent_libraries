"""Private Mautic 6.0.9 web installation; no CLI, logging, or credential storage.

The caller supplies an HTTPS client with in-memory cookies and NO automatic
redirects. Before apply it must independently prove an empty database and the
approved first-run configuration through IAP. An installer page is NOT proof:
Mautic's createAdminUserStep can overwrite user 1 on an incomplete installation.

Source contract (tag 6.0.9 in https://github.com/mautic/mautic):
app/bundles/InstallBundle/{Controller/InstallController.php,
Configurator/Form/{Check,Doctrine,User}StepType.php,Install/InstallService.php}
app/bundles/UserBundle/{Resources/views/Security/login.html.twig,
Controller/ProfileController.php,Form/Type/UserType.php}
"""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


ERROR = "Mautic initialization refused; manual review required."
REDIRECTS = (301, 302, 303, 307, 308)
DASHBOARD = "/s/dashboard"
CHECK = "install_check_step"
DATABASE = "install_doctrine_step"
ADMIN = "install_user_step"


def _require(condition):
    if not condition:
        raise RuntimeError(ERROR)


def _target(client, source, location):
    """Resolve a form/redirect without ever accepting another origin or query."""
    _require(isinstance(location, str) and bool(location))
    _require(not any(ord(char) <= 32 or ord(char) == 127 for char in location))
    _require(not any(char in location for char in ("\\", "%", "?", "#")))
    parsed = urlsplit(urljoin(client.origin + source, location))
    _require(parsed.scheme == "https" and parsed.netloc == client.host)
    _require(parsed.username is None and parsed.password is None)
    _require(not parsed.query and not parsed.fragment)
    return parsed.path


def _redirect(client, response, source, expected):
    # Never replay a POST (307/308), or follow arbitrary same-origin endpoints.
    _require(response.status in (302, 303))
    locations = response.headers.get_all("Location", [])
    _require(len(locations) == 1)
    path = _target(client, source, locations[0])
    _require(path in expected)
    return path


class _Form(HTMLParser):
    """Read successful controls, retaining server-rendered defaults and CSRF."""

    def __init__(self, name):
        super().__init__(convert_charrefs=True)
        self.name = name
        self.matches = 0
        self.active = False
        self.attributes = {}
        self.values = {}
        self.select = None
        self.option = None
        self.textarea = None

    def _add(self, name, value):
        _require(name not in self.values)
        self.values[name] = value

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "base":
            _require(False)
        if tag == "form":
            _require(not self.active)
            self.active = self.name in (attrs.get("name"), attrs.get("id"))
            if self.active:
                self.matches += 1
                self.attributes = attrs
        if not self.active:
            return
        # Mautic 6.0.9 renders autocomplete twice on its database-password
        # control. It is a browser hint, not submitted state. All submission
        # and routing attributes must remain unambiguous.
        names = [key for key, _ in attributes if key != "autocomplete"]
        _require(len(set(names)) == len(names))
        _require(not any(key in attrs for key in ("form", "formaction", "formmethod")))
        if tag == "fieldset":
            _require("disabled" not in attrs)
        if tag in ("input", "button") and attrs.get("name") and "disabled" not in attrs:
            kind = attrs.get("type", "submit" if tag == "button" else "text").lower()
            if kind in ("reset", "button"):
                return
            _require(kind not in ("file", "image"))
            if kind in ("checkbox", "radio") and "checked" not in attrs:
                return
            default = "on" if kind in ("checkbox", "radio") else ""
            self._add(attrs["name"], attrs.get("value", default))
        elif tag == "select":
            _require(self.select is None and "multiple" not in attrs)
            self.select = (attrs, [])
        elif tag == "option" and self.select is not None:
            _require(self.option is None)
            self.option = (attrs, [])
        elif tag == "textarea":
            _require(self.textarea is None)
            self.textarea = (attrs, [])

    def handle_data(self, data):
        if self.option is not None:
            self.option[1].append(data)
        if self.textarea is not None:
            self.textarea[1].append(data)

    def handle_endtag(self, tag):
        if not self.active:
            return
        if tag == "option" and self.option is not None:
            attrs, text = self.option
            if "disabled" not in attrs:
                self.select[1].append((attrs.get("value", "".join(text)), "selected" in attrs))
            self.option = None
        elif tag == "select" and self.select is not None:
            attrs, options = self.select
            _require(self.option is None)
            if attrs.get("name") and "disabled" not in attrs:
                selected = [value for value, chosen in options if chosen]
                _require(len(selected) <= 1 and bool(options))
                self._add(attrs["name"], selected[0] if selected else options[0][0])
            self.select = None
        elif tag == "textarea" and self.textarea is not None:
            attrs, text = self.textarea
            if attrs.get("name") and "disabled" not in attrs:
                self._add(attrs["name"], "".join(text))
            self.textarea = None
        elif tag == "form":
            _require(self.select is None and self.textarea is None)
            self.active = False


def _form(client, response, source, name, action):
    _require(response.status == 200)
    parsed = _Form(name)
    parsed.feed(response.body.decode("utf-8"))
    parsed.close()
    _require(parsed.matches == 1 and not parsed.active)
    _require(parsed.attributes.get("method", "get").lower() == "post")
    _require(parsed.attributes.get("enctype", "application/x-www-form-urlencoded") ==
             "application/x-www-form-urlencoded")
    _require(_target(client, source, parsed.attributes.get("action", source)) == action)
    return parsed.values


def _installer_form(client, response, source, name, action, fields):
    values = _form(client, response, source, name, action)
    expected = {f"{name}[{field}]" for field in (*fields, "_token")}
    # Symfony's next button is optional in a submitted form, but all actual
    # configuration controls must be present. Unknown settings fail closed.
    button = f"{name}[buttons][next]"
    _require(set(values) - {button} == expected)
    _require(bool(values[f"{name}[_token]"]))
    return values


def _post(client, path, values):
    return client.request("POST", path, form=values, headers={
        "Origin": client.origin,
        "Referer": client.origin + path,
    })


def _locked(client, response):
    # InstallController checks persisted db_driver/site_url before consulting
    # authentication or installer session state. Do not follow this redirect.
    _redirect(client, response, "/installer", {DASHBOARD})


def _login(client, credentials):
    path = "/s/login"
    values = _form(client, client.request("GET", path), path, "login", "/s/login_check")
    _require(set(values) == {"_username", "_password", "_csrf_token"})
    _require(bool(values["_csrf_token"]))
    values.update(_username=credentials["username"], _password=credentials["password"])
    response = _post(client, "/s/login_check", values)
    _redirect(client, response, "/s/login_check", {DASHBOARD, "/s/", "/s/account"})
    # A redirect/200 alone is not authentication evidence. Read the supported
    # account form and verify this session belongs to the requested account.
    path = "/s/account"
    account = _form(client, client.request("GET", path), path, "user", path)
    _require(account.get("user[username]") == credentials["username"])
    _require(account.get("user[email]") == credentials["email"])


def initialize(client, credentials, db_password, apply=False, fresh_install_verified=False):
    """Preview one read-only page, or install once using independent caller proof.

    ``fresh_install_verified is True`` attests that the caller has just checked
    DB table_count == 0 AND the exact protected proxy seed plus absent config or
    recognized image first-run DB config (pdo_mysql/env DB fields, no site_url). This function
    never obtains or substitutes that proof, resumes a partial install, forces
    overwrites, resets accounts, configures SMTP, or sends mail. A failed apply
    needs new independent inspection, not a blind retry with the previous flag.

    Credentials is a mapping: email, username, password, first_name, last_name.
    Returned values are fixed booleans only. All failures use a fixed safe error;
    response content, credentials and transport exception text are never emitted.
    """
    try:
        _require(type(apply) is bool)
        if apply:
            _require(fresh_install_verified is True)
            for field in ("email", "username", "password", "first_name", "last_name"):
                _require(isinstance(credentials[field], str) and bool(credentials[field]))
            _require(isinstance(db_password, str) and bool(db_password))
        origin = urlsplit(client.origin)
        _require(origin.scheme == "https" and origin.netloc == client.host)
        _require(origin.hostname == client.host and origin.port is None)
        _require(client.origin == "https://" + client.host)

        response = client.request("GET", "/installer")
        if response.status in REDIRECTS:
            _locked(client, response)
            _require(not apply)  # Preserve installed accounts; never reinstall.
            return {"preview": True, "initialized": False, "installer_available": False,
                    "installer_locked": True, "login_verified": False}
        values = _installer_form(client, response, "/installer", CHECK, "/installer/step/0",
                                 ("site_url", "cache_path", "log_path"))
        _require(values[f"{CHECK}[site_url]"] == client.origin)
        if not apply:
            return {"preview": True, "initialized": False, "installer_available": True,
                    "installer_locked": False, "login_verified": False}

        # The exact state machine deliberately avoids generic redirect following:
        # even same-origin GETs at 1.1, 1.2 and final mutate persistent state.
        response = _post(client, "/installer/step/0", values)
        path = _redirect(client, response, "/installer/step/0", {"/installer/step/1"})
        response = client.request("GET", path)
        values = _installer_form(client, response, path, DATABASE, path,
                                 ("driver", "host", "port", "name", "table_prefix", "user",
                                  "password", "backup_tables", "backup_prefix"))
        # Submit rendered defaults, not copied optional settings. An alternate
        # DB port would invalidate the caller's proof about this stack's DB;
        # refuse it rather than replacing an operator's choice. Also refuse the
        # destructive drop-tables option even with the empty-DB proof.
        _require(values[f"{DATABASE}[driver]"] == "pdo_mysql")
        _require(values[f"{DATABASE}[port]"] == "3306")
        _require(values[f"{DATABASE}[backup_tables]"] == "1")
        for key, value in {"host": "mautic-mariadb", "name": "mautic", "user": "mautic",
                           "password": db_password}.items():
            values[f"{DATABASE}[{key}]"] = value
        response = _post(client, path, values)
        path = _redirect(client, response, path, {"/installer/step/1.1"})
        response = client.request("GET", path)
        path = _redirect(client, response, path, {"/installer/step/1.2"})
        response = client.request("GET", path)
        path = _redirect(client, response, path, {"/installer/step/2"})
        response = client.request("GET", path)
        values = _installer_form(client, response, path, ADMIN, path,
                                 ("firstname", "lastname", "email", "username", "password"))
        for field, key in (("firstname", "first_name"), ("lastname", "last_name"),
                           ("email", "email"), ("username", "username"), ("password", "password")):
            values[f"{ADMIN}[{field}]"] = credentials[key]
        response = _post(client, path, values)
        path = _redirect(client, response, path, {"/installer/final"})
        _require(client.request("GET", path).status == 200)
        # Still unauthenticated: check lockout before submitting the login form.
        _locked(client, client.request("GET", "/installer"))
        _login(client, credentials)
        return {"preview": False, "initialized": True, "installer_available": False,
                "installer_locked": True, "login_verified": True}
    except Exception:
        raise RuntimeError(ERROR) from None