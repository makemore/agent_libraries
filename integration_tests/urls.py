"""Test-host mounts of real views; no changes to production URL/auth policy."""

from django.urls import include, path
from django_agent_runtime.api.views import (
    BaseAgentDefinitionViewSet,
    BaseAgentRunViewSet,
)
from django_agent_studio.api.views import AgentDefinitionListCreateView
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class RuntimeAgents(BaseAgentDefinitionViewSet):
    # Base runtime views intentionally delegate authentication to the host.
    # These explicit test-host policies are checked by the token rejection tests.
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)


class RuntimeRuns(BaseAgentRunViewSet):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)


class SetupAgents(AgentDefinitionListCreateView):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)


class Identity(APIView):
    """Optional host identity capability, not a new runtime/Studio endpoint."""

    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response({"id": str(request.user.pk), "username": request.user.get_username()})


runtime_patterns = [
    path("agents/", RuntimeAgents.as_view({"get": "list"})),
    path("agents/<slug:slug>/", RuntimeAgents.as_view({"get": "retrieve"})),
    path("runs/", RuntimeRuns.as_view({"get": "list", "post": "create"})),
    # Deliberately mount the real unsafe retrieve too: the adapter must refuse
    # it before transport, rather than merely relying on an absent URL.
    path("runs/<uuid:pk>/", RuntimeRuns.as_view({"get": "retrieve"})),
]

urlpatterns = [
    path("api/agent-runtime/", include(runtime_patterns)),
    path("alternate/runtime/", include(runtime_patterns)),
    path("studio/api/workspace/", include("django_agent_studio.api.workspace_urls")),
    path("alternate/studio/api/workspace/", include(
        "django_agent_studio.api.workspace_urls", namespace="alternate_workspace",
    )),
    path("setup/agents/", SetupAgents.as_view()),
    path("host/identity/", Identity.as_view()),
]
