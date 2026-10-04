"""In-memory, process-local registry of active device connections.

    device_uuid (str) -> DeviceSession

Never persisted to PostgreSQL and never serialized — a Python socket object
has no business in a database row. Process-local by design; see this
package's __init__.py docstring for the documented single-instance
deployment limitation this implies.
"""


class ConnectionManager:
    def __init__(self):
        self._sessions: dict[str, "DeviceSession"] = {}  # noqa: F821 (forward ref, see device_session.py)

    def register(self, session):
        self._sessions[str(session.device.uuid)] = session

    def unregister(self, session):
        current = self._sessions.get(str(session.device.uuid))
        if current is session:
            del self._sessions[str(session.device.uuid)]

    def get(self, device_uuid):
        return self._sessions.get(str(device_uuid))

    def is_connected(self, device_uuid):
        return str(device_uuid) in self._sessions

    def connected_device_ids(self):
        """Integer PKs (not uuids) — feeds DeviceCommand's
        ``device_id__in=...`` claim query directly."""
        return [session.device.pk for session in self._sessions.values()]

    def all_sessions(self):
        return list(self._sessions.values())

    def __len__(self):
        return len(self._sessions)
