"""Standalone asyncio TCP service for GPS/telematics device connections.

Runs as a SEPARATE OS PROCESS from the Django web app (started via
``python manage.py run_telematics_server``), coordinating with it only
through PostgreSQL — never in-memory, never WebSockets/Channels/Redis.

Deployment limitation (documented, not solved here): this package assumes
exactly ONE running instance. Its connection registry
(``connection_manager.ConnectionManager``) is process-local, in-memory
state — a second instance would have its own, disjoint registry, so a
command claimed by instance A can only ever be sent through a session
A itself holds. Running multiple instances safely would need a shared
session registry (e.g. Redis) and is explicitly out of scope for this
phase; do not deploy more than one instance at a time.
"""
