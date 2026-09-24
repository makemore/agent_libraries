from django.urls import path

from . import views

app_name = "django_agent_channels"

urlpatterns = [
    path("agentmail/", views.agentmail_webhook, name="agentmail-webhook"),
    path("twilio/", views.twilio_webhook, name="twilio-webhook"),
]
