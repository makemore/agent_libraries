# Raise CRM MCP: first working slice

## Available tools

| Tool | Purpose |
| --- | --- |
| `list_profiles` | Discover the authenticated user's active grants. |
| `list_contacts` | Search and paginate contacts in a profile's charity. |
| `get_contact` | Contact details, contact methods and cached web summary. |
| `create_contact` | Create a person, company, trust or foundation. |
| `add_contact_method` | Add an email address, phone number or postal address. |
| `log_interaction` | Record correspondence, calls, meetings, notes or substantive web enquiries. |
| `contact_timeline` | Read a paginated timeline, without large message bodies. |
| `get_interaction` | Read original content and historical participant snapshots. |

Logging does **not** send email, make calls, process donations or imply consent.
Raw pageviews are not CRM interactions. For web interactions, `purpose` must be
`enquiry`, `application` or `event_registration`.

## Identity and authorization

- `User` authenticates a human or agent. `Profile` is a charity-scoped acting identity.
- `ProfileGrant` authorizes a user to act through a profile as `viewer`, `fundraiser` or `admin`.
- Every CRM call requires an explicit `profile_id`; there is no global mutable active profile.
- An admin grant is charity/profile access, not Django superuser access. Superusers do not bypass grants in MCP.
- Restricted interactions are visible through their originating profile, or an admin grant in that charity.
- Mutations record both user and profile. Idempotency keys are required; reuse a key only for the same request.
- Token validity, user activity and profile permissions are checked on each call.

## Local setup

Run from `backend/`, with Python 3.11+:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
```

Configure the intended CRM database through existing Django settings. PostgreSQL
is the application database. For a **local-only** contact/MCP trial, `export
USE_SQLITE=true` selects the existing SQLite option. Do not use test settings or
their test-only signing key for a real account.

The following steps write to that configured database. Use a local/development
database first. Create an initial login interactively, without putting a password
in shell history (an existing user can be used instead):

```sh
.venv/bin/python manage.py migrate --database=default
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py bootstrap_raise --user-email YOU@example.org --charity-name "My charity" --profile-name "Fundraising" --currency GBP
```

`bootstrap_raise` is a trusted **host-administrator** command, not an MCP tool. It
creates a charity, profile and admin grant for an existing active user. Further
users can be granted access to an existing profile:

```sh
.venv/bin/python manage.py grant_profile --host-admin --user-email AGENT@example.org --profile-id PROFILE_UUID --role fundraiser
```

Export that user's token into a **new** file in an owner-only directory you control.
Replace the path below; the command never prints the credential or overwrites a file:

```sh
.venv/bin/python manage.py issue_mcp_token --user-email YOU@example.org --token-file /absolute/private/raise-crm.token
```

The file must remain owned by the process user with mode 0600 or 0400. Symlinks
are rejected. The exported DRF token has no trailing newline. Never paste its
contents into an agent prompt, tool argument, source file or MCP configuration.
DRF currently uses one token per user; deleting that user's Token row through a
trusted admin shell revokes all clients using it. This is local token auth, not
remote OAuth or per-client expiring credentials.

## Connect a local MCP client

Use a stdio connection. Most clients accept a server entry with these fields;
replace the absolute paths. Add it to the client's MCP server configuration:

```json
{
  "command": "/absolute/path/to/raise2026/backend/.venv/bin/python",
  "args": ["/absolute/path/to/raise2026/backend/manage.py", "run_mcp", "--token-file", "/absolute/private/raise-crm.token"],
  "cwd": "/absolute/path/to/raise2026/backend",
  "env": {"DJANGO_SETTINGS_MODULE": "raise.settings.base", "USE_SQLITE": "true"}
}
```

`USE_SQLITE=true` above is **only for the local trial**. Remove it when using
PostgreSQL. Keep connection secrets in existing protected environment/settings,
not inline client configuration. Some clients require a surrounding `mcpServers`
map or different configuration syntax. The client starts/stops the process; there
is no background daemon or HTTP URL to connect to in this build.

Start by asking the agent to list profiles, select an authorized profile, then
create a contact and log a call. Input schemas are provided by tool discovery.

## Separate engagement database

The engagement store is optional for contact-only use. With no configured alias,
the CRM runs but event services fail closed: they never use the main database.
Set `ENGAGEMENT_DATABASE_URL` through protected environment configuration to a
**different PostgreSQL instance**, then run:

```sh
.venv/bin/python manage.py migrate --database=engagement
```

Routing and checks prevent engagement tables from migrating into the default DB.
Different database names on the same host/port are rejected; DNS aliases/proxies
cannot reliably prove instance isolation, so deployment configuration matters.
Only scalar references cross stores. Ingestion writes no per-event CRM rows;
summary refreshes update bounded contact summaries separately.

## Verification and remaining scope

```sh
.venv/bin/python manage.py test crm engagement --settings=raise.settings.test --noinput
.venv/bin/python manage.py makemigrations --check --dry-run --settings=raise.settings.test
.venv/bin/python -m pip check
```

Tests use disposable SQLite databases and include a real stdio subprocess/client
round trip. PostgreSQL concurrency/load tests still need separate infrastructure.

Models/migrations exist for funds, appeals, opportunities, pledges, donations,
consent, attachments and tasks; their complete services and MCP tools are **not
part of this first slice**. No frontend, CRM REST endpoints, remote MCP endpoint,
browser tracker, email integration, ingestion queue, retention worker, partition
maintenance or BigQuery adapter is shipped yet. Event URLs currently retain only
the registered origin; page-level analysis needs an approved route-template scheme.
Do not treat the event store as production-scale analytics before that work.