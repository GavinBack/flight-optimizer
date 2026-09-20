import pytest
import requests

from flight_optimizer.notify import MAX_MESSAGE_BYTES, NtfyClient, NtfyError

TOPIC = "secret-topic-abc123"


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class FakeSession:
    def __init__(self, outcome=None):
        self.outcome = outcome or FakeResponse()
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make(outcome=None, **kwargs):
    session = FakeSession(outcome)
    return NtfyClient(TOPIC, session=session, **kwargs), session


def test_publishes_json_to_the_server_root_with_the_topic_in_the_body():
    client, session = make()

    client.send("hello", title="A title", priority=4, tags=("airplane", "warning"), click="https://x.test/book")

    post = session.posts[0]
    assert post["url"] == "https://ntfy.sh"
    assert post["json"] == {
        "topic": TOPIC, "message": "hello", "priority": 4, "title": "A title",
        "tags": ["airplane", "warning"], "click": "https://x.test/book",
    }


def test_optional_fields_are_left_out_when_unused():
    client, session = make()

    client.send("hello")

    assert session.posts[0]["json"] == {"topic": TOPIC, "message": "hello", "priority": 3}


def test_a_self_hosted_server_can_be_used_and_a_trailing_slash_is_ignored():
    client, session = make(server="https://ntfy.example.org/")

    client.send("hi")

    assert session.posts[0]["url"] == "https://ntfy.example.org"


def test_non_ascii_text_is_fine_because_it_travels_as_json():
    client, session = make()

    client.send("2.5¢ per mile ✈", title="Costa Rica – alert")

    assert session.posts[0]["json"]["message"].startswith("2.5¢")


def test_topic_is_required():
    with pytest.raises(ValueError):
        NtfyClient("")


def test_messages_over_the_ntfy_limit_are_refused_not_turned_into_attachments():
    client, session = make()

    with pytest.raises(ValueError, match="4096"):
        client.send("x" * (MAX_MESSAGE_BYTES + 1))
    assert session.posts == []


def test_the_limit_is_in_bytes_not_characters():
    client, _ = make()

    with pytest.raises(ValueError):
        client.send("é" * (MAX_MESSAGE_BYTES // 2 + 1))  # 2 bytes each


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_http_errors_raise_without_leaking_the_topic(status):
    client, _ = make(FakeResponse(status))

    with pytest.raises(NtfyError) as excinfo:
        client.send("hello")

    assert str(status) in str(excinfo.value) and TOPIC not in str(excinfo.value)


def test_network_errors_raise_without_the_underlying_text():
    leaky = requests.ConnectionError(f"failed posting {TOPIC} to https://ntfy.sh")
    client, _ = make(leaky)

    with pytest.raises(NtfyError) as excinfo:
        client.send("hello")

    assert TOPIC not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
