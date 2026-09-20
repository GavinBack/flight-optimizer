"""Send push notifications through ntfy (https://ntfy.sh, or a self-hosted server).

Messages are published as JSON to the server root, so the topic travels in the body
and titles or messages can contain any characters (HTTP headers can't). Errors are
re-raised without the underlying exception text, which could echo request details.
"""

import requests

DEFAULT_SERVER = "https://ntfy.sh"
MAX_MESSAGE_BYTES = 4096  # ntfy turns anything longer into a file attachment


class NtfyError(Exception):
    pass


class NtfyClient:
    def __init__(
        self,
        topic: str,
        *,
        server: str = DEFAULT_SERVER,
        session: requests.Session | None = None,
        timeout: float = 15,
    ):
        if not topic:
            raise ValueError("topic is required")
        self._topic = topic
        self._server = server.rstrip("/")
        self._session = session or requests.Session()
        self._timeout = timeout

    def send(
        self,
        message: str,
        *,
        title: str | None = None,
        priority: int = 3,
        tags: tuple[str, ...] = (),
        click: str | None = None,
    ) -> None:
        """Publish one notification. `priority` is ntfy's 1 (min) to 5 (max)."""
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message is over ntfy's {MAX_MESSAGE_BYTES}-byte limit")
        payload: dict = {"topic": self._topic, "message": message, "priority": priority}
        if title:
            payload["title"] = title
        if tags:
            payload["tags"] = list(tags)
        if click:
            payload["click"] = click

        try:
            response = self._session.post(self._server, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise NtfyError(f"ntfy request failed ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise NtfyError(f"ntfy returned HTTP {response.status_code}")
