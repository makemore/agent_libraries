"""Isolated first-build engagement storage and transport-independent services.

No browser tracker, HTTP endpoint, queue, retention worker, WebSession table, or
PostgreSQL partition DDL is included. Provision/migrate the separate database and
wire the app/router/settings helper explicitly before using these services.
Retention, deletion/erasure and operational capacity policies are deployment
prerequisites, not background work implemented here. Client event UUIDs are
globally unique; collisions across charities yield only a generic conflict.
"""