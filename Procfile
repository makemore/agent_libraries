frontend: cd clients/agent-frontend && npm run watch -- --studio
studio: cd agent/django_agent_studio/frontend && npm run build:watch
django: cd agent/agent_studio && DJANGO_SETTINGS_MODULE=agent_studio.settings.dev PYTHONUNBUFFERED=1 .venv/bin/python manage.py runserver 127.0.0.1:8001
agent: cd agent/agent_studio && DJANGO_SETTINGS_MODULE=agent_studio.settings.dev PYTHONUNBUFFERED=1 .venv/bin/python manage.py runagent

