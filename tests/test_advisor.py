from datetime import date

import pytest
import requests

from builders import add_trip, analysis_for, good_option
from flight_optimizer import db
from flight_optimizer.advisor import (
    SYSTEM_PROMPT, AdvisorError, AnthropicClient, build_brief, estimate_tokens,
)
from flight_optimizer.seats_aero import Cabin

KEY = "sk-ant-test-key-123"
TODAY = date(2026, 9, 21)


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def reply(text="Book economy.", stop="end_turn", **extra):
    return {"model": "claude-sonnet-5", "stop_reason": stop,
            "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1200, "output_tokens": 300}, **extra}


class FakeSession:
    def __init__(self, outcome=None):
        self.outcome = outcome or FakeResponse(reply())
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make(outcome=None, **kwargs):
    session = FakeSession(outcome)
    return AnthropicClient(KEY, session=session, **kwargs), session


# --- the request -------------------------------------------------------------------------------------------

def test_the_request_carries_the_key_version_model_system_prompt_and_brief():
    client, session = make(model="claude-opus-5")

    client.advise("the brief")

    (post,) = session.posts
    assert post["url"] == "https://api.anthropic.com/v1/messages"
    assert post["headers"]["x-api-key"] == KEY and post["headers"]["anthropic-version"] == "2023-06-01"
    assert post["json"]["model"] == "claude-opus-5"
    assert post["json"]["system"] == SYSTEM_PROMPT
    assert post["json"]["messages"] == [{"role": "user", "content": "the brief"}]
    assert 4000 <= post["json"]["max_tokens"] <= 16000   # room to think as well as answer


def test_the_key_never_appears_in_the_body_that_is_sent():
    client, session = make()

    client.advise("the brief")

    assert KEY not in str(session.posts[0]["json"])


def test_an_api_key_is_required():
    with pytest.raises(ValueError):
        AnthropicClient("")


# --- the answer --------------------------------------------------------------------------------------------

def test_the_text_and_token_counts_come_back():
    advice = make()[0].advise("x")

    assert advice.text == "Book economy." and advice.model == "claude-sonnet-5"
    assert (advice.input_tokens, advice.output_tokens, advice.truncated) == (1200, 300, False)


def test_several_text_blocks_are_joined_and_other_blocks_ignored():
    body = reply()
    body["content"] = [{"type": "text", "text": "One."}, {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": " Two."}]

    assert make(FakeResponse(body))[0].advise("x").text == "One. Two."


def test_a_reply_cut_off_at_the_limit_is_marked():
    assert make(FakeResponse(reply(stop="max_tokens")))[0].advise("x").truncated is True


def test_running_out_of_tokens_while_thinking_says_so():
    body = reply(stop="max_tokens")
    body["content"] = [{"type": "thinking", "thinking": ""}]

    with pytest.raises(AdvisorError, match="used all 1,000 output tokens before answering"):
        make(FakeResponse(body))[0].advise("x", max_tokens=1000)


@pytest.mark.parametrize("body", [{}, {"content": []}, {"content": [{"type": "text", "text": "  "}]}, "not json", None])
def test_an_empty_or_unreadable_reply_is_an_error(body):
    if body == "not json":
        body = ValueError("bad json")
    with pytest.raises(AdvisorError):
        make(FakeResponse(body))[0].advise("x")


# --- failures ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status, hint", [
    (401, "ANTHROPIC_API_KEY"), (404, "ANTHROPIC_MODEL"), (429, "credit"), (529, "overloaded"), (500, "request failed"),
])
def test_http_errors_say_what_to_check(status, hint):
    with pytest.raises(AdvisorError, match=f"HTTP {status}") as excinfo:
        make(FakeResponse({"error": {"message": KEY}}, status_code=status))[0].advise("x")

    assert hint in str(excinfo.value)
    assert KEY not in str(excinfo.value)     # whatever the server echoed is not repeated


def test_network_errors_do_not_leak_request_details():
    error = requests.ConnectionError(f"failed for header x-api-key: {KEY}")

    with pytest.raises(AdvisorError) as excinfo:
        make(error)[0].advise("x")

    assert "ConnectionError" in str(excinfo.value) and KEY not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


# --- the brief ---------------------------------------------------------------------------------------------

@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def trip_analyses(conn, **overrides):
    trip = add_trip(conn, **overrides)
    return [
        analysis_for(trip, [], cabin=Cabin.ECONOMY),
        analysis_for(trip, [good_option()], cabin=Cabin.BUSINESS),
    ]


def test_the_brief_holds_the_date_balances_comparison_and_each_cabin(conn):
    brief = build_brief([trip_analyses(conn)], {"capital_one": 115_519, "delta": 14_049, "united": 0}, today=TODAY)

    assert "Today is Monday 2026-09-21." in brief
    assert "Capital One Miles 115,519" in brief and "Delta SkyMiles 14,049" in brief
    assert "United MileagePlus" not in brief   # zero balances are left out
    assert "CABINS AT A GLANCE" in brief
    assert "(economy)" in brief and "(business)" in brief
    assert "71,800 miles" in brief


def test_every_trip_is_in_one_brief_so_the_shared_pool_can_be_weighed(conn):
    first = trip_analyses(conn)
    second = trip_analyses(conn, name="Kelowna", origin="RNO", destination="YLW")

    brief = build_brief([first, second], {"capital_one": 100_000}, today=TODAY)

    assert "Costa Rica" in brief and "Kelowna" in brief


def test_the_brief_is_trimmed_to_the_leading_options(conn):
    options = [good_option(out_miles=10_900 + 1_000 * i, flights=(f"A{i}", f"B{i}", f"C{i}", f"D{i}")) for i in range(6)]
    trip = add_trip(conn)
    analyses = [analysis_for(trip, options, cabin=Cabin.BUSINESS)]

    brief = build_brief([analyses], {}, today=TODAY)

    assert "#3 " in brief and "#4 " not in brief


def test_the_brief_says_no_balances_when_none_are_set(conn):
    assert "Balances: none set." in build_brief([trip_analyses(conn)], {}, today=TODAY)


def test_the_system_prompt_states_the_owners_rules():
    for fact in ("never book", "reserve", "irreversible", "1 cent", "1.5x", "not instructions", "UNCHECKED".lower(),
                 "Use only figures that appear in the report"):
        assert fact.lower() in SYSTEM_PROMPT.lower()


def test_token_estimates_follow_the_measured_ratio_for_report_text():
    assert estimate_tokens("x" * 2100) == 1000


# --- saved advice ------------------------------------------------------------------------------------------

def test_advice_is_saved_with_its_details_and_the_newest_is_found(tmp_path):
    from datetime import datetime
    from flight_optimizer.advisor import Advice, latest_advice, save_advice

    directory = tmp_path / "advice"
    older = save_advice(Advice("first", "m", 10, 5), generated_at=datetime(2026, 9, 20, 7, 0), directory=directory)
    newer = save_advice(Advice("second", "m", 1_200, 300, truncated=True), generated_at=datetime(2026, 9, 21, 7, 0), directory=directory)

    assert older.name == "advice-20260920-0700.txt"
    assert "1,200 tokens in, 300 out" in newer.read_text() and "cut off" in newer.read_text()
    assert latest_advice(directory) == newer


def test_no_saved_advice_is_none(tmp_path):
    from flight_optimizer.advisor import latest_advice

    assert latest_advice(tmp_path / "missing") is None
    (tmp_path / "advice").mkdir()
    assert latest_advice(tmp_path / "advice") is None


# --- the fingerprint: when is the situation "unchanged"? ---------------------------------------------------------

from datetime import timedelta
from dataclasses import replace

from flight_optimizer.advisor import CASH_STEP, advice_date, fingerprint
from flight_optimizer.promos import Promo
from flight_optimizer.savings import TripBaseline
from builders import fare

BAL = {"capital_one": 100_000, "delta": 30_000}


def print_of(analyses, balances=BAL, promos=None, model="m", today=TODAY):
    return fingerprint([analyses], balances, promos, model=model, today=today)


def situation(conn, options=None, cash=(2_267, 699)):
    trip = conn.execute("SELECT * FROM trips ORDER BY id LIMIT 1").fetchone() or add_trip(conn)   # one trip, however often it is asked for
    base = TripBaseline(cabin_fares=(fare(cash[0]),), economy_fares=(fare(cash[1]),))
    return [analysis_for(trip, options if options is not None else [good_option()], baseline_value=base, cabin=Cabin.BUSINESS)]


def test_the_same_situation_has_the_same_fingerprint(conn):
    assert print_of(situation(conn)) == print_of(situation(conn))


def test_time_passing_alone_is_not_a_change(conn):
    # The date and the number of days old the data is tick up daily; neither changes the picture.
    assert print_of(situation(conn), today=TODAY) == print_of(situation(conn), today=TODAY + timedelta(days=1))


def test_data_crossing_into_a_worse_freshness_tier_is_a_change(conn):
    assert print_of(situation(conn), today=date.today()) != print_of(situation(conn), today=date.today() + timedelta(days=9))


def test_small_fare_moves_are_ignored_and_big_ones_are_not(conn):
    base = print_of(situation(conn, cash=(2_267, 699)))

    assert print_of(situation(conn, cash=(2_270, 701))) == base
    assert print_of(situation(conn, cash=(2_267 + CASH_STEP * 2, 699))) != base


@pytest.mark.parametrize("change", [
    lambda conn: dict(balances={**BAL, "delta": 31_000}),
    lambda conn: dict(balances={"capital_one": 100_000}),
    lambda conn: dict(model="another-model"),
    lambda conn: dict(analyses=situation(conn, options=[good_option(out_miles=12_000)])),
    lambda conn: dict(analyses=situation(conn, options=[])),
    lambda conn: dict(analyses=situation(conn, options=[good_option(flights=("X1", "X2", "X3", "X4"))])),
    lambda conn: dict(promos={"delta": Promo(key="k", program="delta", kind="bonus", headline_percent=50, tiers=(),
                                             ends=TODAY + timedelta(days=5), title="t", url="u", source="s", last_seen=TODAY)}),
])
def test_anything_the_advice_depends_on_changes_the_fingerprint(conn, change):
    args = dict(analyses=situation(conn), balances=BAL, promos=None, model="m")
    baseline_print = print_of(**args)

    assert print_of(**{**args, **change(conn)}) != baseline_print


def test_a_sale_seen_again_is_not_a_change(conn):
    def sale(seen):
        return {"delta": Promo(key="k", program="delta", kind="bonus", headline_percent=50, tiers=(),
                               ends=TODAY + timedelta(days=5), title="t", url="u", source="s", last_seen=seen)}

    analyses = situation(conn)

    assert print_of(analyses, promos=sale(TODAY)) == print_of(analyses, promos=sale(TODAY + timedelta(days=1)))


def test_changing_the_instructions_asks_again(conn, monkeypatch):
    import flight_optimizer.advisor as advisor

    before = print_of(situation(conn))
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT", advisor.SYSTEM_PROMPT + " Be brief.")

    assert print_of(situation(conn)) != before


def test_the_day_of_a_saved_answer_comes_from_its_file_name(tmp_path):
    from pathlib import Path

    assert advice_date(Path("advice-20260921-0700.txt")) == date(2026, 9, 21)


def test_switching_a_trip_to_flexible_asks_again_and_the_prompt_explains_it(conn):
    exact = situation(conn)
    before = print_of(exact)
    db.update_trip(conn, exact[0].trip["id"], flexible=True)
    flexible = situation(conn)

    assert print_of(flexible) != before
    assert "flexible" in SYSTEM_PROMPT and "exact requested dates" in SYSTEM_PROMPT
