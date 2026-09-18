"""Small supported-write fixtures. Credentials exist only in isolated memory."""

from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

WORKSPACE = "/studio/api/workspace/"


@dataclass
class Actor:
    user: object
    _token: str = field(repr=False)

    @classmethod
    def create(cls, username):
        user = get_user_model().objects.create_user(username=username)
        return cls(user, Token.objects.create(user=user).key)

    def request(self, method, path, data=None, *, status):
        """Setup only; all reads under test use Client/ReadAPI instead."""
        __tracebackhide__ = True
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {self._token}")
        response = getattr(client, method)(path, data or {}, format="json")
        assert response.status_code == status, "Supported fixture API returned an unexpected status"
        return response.json() if status != 204 else None

    def workspace(self, method, path, data=None, *, status):
        return self.request(method, WORKSPACE + path, data, status=status)

    def revoke_token(self):
        # Keep the old value in this actor so subsequent requests prove liveness.
        Token.objects.filter(user=self.user).delete()

    def deactivate(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])


def create_agent(actor, slug):
    return actor.request("post", "/setup/agents/", {"name": slug, "slug": slug}, status=201)


def create_organization(actor, name):
    return actor.workspace("post", "organizations/", {"name": name}, status=201)


def create_project(actor, organization, name):
    return actor.workspace("post", "projects/", {
        "organization_id": organization["id"], "name": name,
    }, status=201)


def create_thread(actor, project, agent, *, title="Contract thread"):
    # No storage/history, retention, target, visibility, model or queue overrides.
    return actor.workspace("post", "threads/", {
        "project_id": project["id"], "agent_key": agent["slug"], "title": title,
    }, status=201)


def project_member(owner, project, actor, *, role="viewer"):
    return owner.workspace("post", f"projects/{project['id']}/members/", {
        "username": actor.user.get_username(), "role": role,
    }, status=201)
