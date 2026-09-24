"""Agent tools. The acting agent always comes from the run (``ctx.identity.agent``);
the model can never choose which agent it acts as.

Register them on an agent's tool registry::

    from django_agent_channels.tools import channel_tools
    for tool in channel_tools():
        registry.register(tool)

Results are plain dicts. Inbound email/SMS text is returned inside
``untrusted_content`` so the agent treats it as data, not instructions.
"""

import functools
import uuid

from agent_runtime_core.identity import get_run_identity
from agent_runtime_core.interfaces import Tool
from asgiref.sync import sync_to_async

from . import domains, email, policy, sms
from .errors import ChannelError, PermissionDenied


def _agent(ctx):
    identity = get_run_identity(ctx)
    if identity is None or identity.agent is None:
        raise PermissionDenied("This run has no agent identity.")
    return identity.agent.key


def _key(ctx, value):
    """Idempotency: the model may pass a key; default is stable per run + tool call."""
    if value:
        return str(value)[:200]
    return f"{getattr(ctx, 'run_id', '')}:{uuid.uuid4()}"


def _message(m):
    data = {"id": str(m.id), "direction": m.direction, "status": m.status,
            "from" if m.direction == "in" else "to": m.counterparty if m.direction == "in" else m.recipients,
            "subject": m.subject, "thread": m.thread_ref, "at": m.created_at.isoformat()}
    if m.direction == "in":
        data["untrusted_content"] = m.body
    else:
        data["body"] = m.body
    return data


def _tool(name, description, properties, required=()):
    def decorator(func):
        @functools.wraps(func)
        async def handler(ctx=None, **kwargs):
            if ctx is None:
                return {"error": "missing run context"}
            try:
                return await sync_to_async(func, thread_sensitive=False)(ctx, **kwargs)
            except ChannelError as exc:
                return {"error": exc.code, "message": exc.message, **({"fields": exc.fields} if exc.fields else {})}

        return Tool(name=name, description=description, handler=handler, has_side_effects=True,
                    parameters={"type": "object", "properties": properties, "required": list(required)},
                    source="static", metadata={"requires_context": True, "package": "django_agent_channels"})
    return decorator


IDEMPOTENCY = {"type": "string", "description": "Reuse the same value to safely retry this exact action."}


@_tool("channels_status", "Show your channel permissions, spending this month, inboxes, numbers and domains.", {})
def channels_status(ctx):
    agent = _agent(ctx)
    current = policy.get_policy(agent)
    return {
        "agent": agent,
        "enabled": bool(current and current.enabled),
        "allowed": {"email": bool(current and current.can_email),
                    "buy_domains": bool(current and current.can_buy_domains),
                    "sms": bool(current and current.can_sms)},
        **policy.summary(agent),
        "inboxes": [e.address for e in email.list_inboxes(agent=agent)],
        "numbers": [e.address for e in sms.list_numbers(agent=agent)],
        "domains": [{"domain": d.name, "status": d.status, "detail": d.detail}
                    for d in domains.list_domains(agent=agent)],
    }


@_tool("search_domains", "Search available domains with live prices (USD cents).",
       {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])
def search_domains(ctx, query, limit=10):
    return {"results": domains.search_domains(agent=_agent(ctx), query=query, limit=limit)}


@_tool("buy_domain", "Buy a domain with your budget and set it up for email (DNS + verification). "
       "Call again later to continue setup; it never buys twice for the same idempotency_key.",
       {"domain": {"type": "string"}, "idempotency_key": IDEMPOTENCY}, ["domain", "idempotency_key"])
def buy_domain(ctx, domain, idempotency_key):
    row = domains.buy_domain(agent=_agent(ctx), domain=domain, idempotency_key=idempotency_key)
    return {"domain": row.name, "status": row.status, "detail": row.detail, "price_cents": row.price_cents}


@_tool("check_domain", "Continue setup of a domain you own and report its status.",
       {"domain": {"type": "string"}}, ["domain"])
def check_domain(ctx, domain):
    agent = _agent(ctx)
    row = next((d for d in domains.list_domains(agent=agent) if d.name == domain.strip().lower()), None)
    if row is None:
        return {"error": "not_found", "message": "You don't own that domain."}
    row = domains.advance_domain(domain=row)
    return {"domain": row.name, "status": row.status, "detail": row.detail}


@_tool("create_inbox", "Create an email inbox for yourself, e.g. username 'sales' on a domain you own "
       "(or the default domain).",
       {"username": {"type": "string"}, "domain": {"type": "string"}, "display_name": {"type": "string"}},
       ["username"])
def create_inbox(ctx, username, domain=None, display_name=""):
    endpoint = email.create_inbox(agent=_agent(ctx), username=username, domain=domain,
                                  display_name=display_name)
    return {"inbox": endpoint.address, "id": str(endpoint.id)}


@_tool("send_email", "Send an email from one of your inboxes, or reply in a thread with reply_to_message_id.",
       {"inbox": {"type": "string", "description": "Your inbox address."},
        "to": {"type": "array", "items": {"type": "string"}},
        "cc": {"type": "array", "items": {"type": "string"}},
        "bcc": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"}, "body": {"type": "string"},
        "reply_to_message_id": {"type": "string"}, "idempotency_key": IDEMPOTENCY},
       ["inbox", "body", "idempotency_key"])
def send_email(ctx, inbox, body, idempotency_key, to=(), cc=(), bcc=(), subject="", reply_to_message_id=None):
    message = email.send_email(agent=_agent(ctx), inbox=inbox, to=to, cc=cc, bcc=bcc, subject=subject,
                               body=body, reply_to_message_id=reply_to_message_id,
                               idempotency_key=_key(ctx, idempotency_key))
    return {"id": str(message.id), "status": message.status}


@_tool("read_email", "List recent messages in one of your inboxes (newest first).",
       {"inbox": {"type": "string"}, "direction": {"type": "string", "enum": ["in", "out"]},
        "limit": {"type": "integer"}}, ["inbox"])
def read_email(ctx, inbox, direction=None, limit=20):
    items = email.list_messages(agent=_agent(ctx), inbox=inbox, direction=direction, limit=limit)
    return {"messages": [_message(m) for m in items]}


@_tool("read_thread", "Read a whole email/SMS thread by one of its message ids.",
       {"message_id": {"type": "string"}}, ["message_id"])
def read_thread(ctx, message_id):
    return {"messages": [_message(m) for m in email.get_thread(agent=_agent(ctx), message_id=message_id)]}


@_tool("buy_phone_number", "Buy an SMS-capable phone number with your budget.",
       {"country": {"type": "string", "description": "Two-letter code, default US."},
        "area_code": {"type": "string"}, "idempotency_key": IDEMPOTENCY}, ["idempotency_key"])
def buy_phone_number(ctx, idempotency_key, country="US", area_code=None):
    endpoint = sms.buy_number(agent=_agent(ctx), country=country, area_code=area_code,
                              idempotency_key=idempotency_key)
    return {"number": endpoint.address}


@_tool("send_sms", "Send a text message from one of your numbers.",
       {"from_number": {"type": "string"}, "to": {"type": "string"}, "body": {"type": "string"},
        "idempotency_key": IDEMPOTENCY}, ["from_number", "to", "body", "idempotency_key"])
def send_sms(ctx, from_number, to, body, idempotency_key):
    message = sms.send_sms(agent=_agent(ctx), from_number=from_number, to=to, body=body,
                           idempotency_key=_key(ctx, idempotency_key))
    return {"id": str(message.id), "status": message.status, "error": message.error}


def channel_tools():
    return [channels_status, search_domains, buy_domain, check_domain, create_inbox, send_email,
            read_email, read_thread, buy_phone_number, send_sms]
