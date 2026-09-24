from django.urls import include, path

urlpatterns = [path("agent-channels/webhooks/", include("django_agent_channels.urls"))]
