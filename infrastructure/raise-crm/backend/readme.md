# Raise CRM

## Django + MCP first build

The local stdio MCP server supports profile-scoped contact management and
interaction history. See [MCP setup and connection](docs/MCP.md).

Use Python 3.11+ and install `requirements.lock.txt` for this build. Domain
services are transport-independent so the future REST API can reuse them.

Django project with:

- Django REST Framework
- Django Allauth (email-only auth)
- dj-rest-auth (API endpoints)
- django-authtools (custom User model)
- S3 storage (Backblaze B2)
- django-cloud-tasks (GCP Cloud Tasks queue)

## Quick Start

This project uses [uv](https://github.com/astral-sh/uv) for fast dependency management.

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies (uv is much faster than pip)
uv pip install -r requirements.lock.txt

python manage.py migrate
python manage.py runserver
```