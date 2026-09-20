from datetime import datetime

import pytest

from builders import JAN9, JAN16
from flight_optimizer import db
from flight_optimizer.__main__ import Clients, main
from flight_optimizer.notify import NtfyError
from flight_optimizer.seats_aero import AwardResult, Cabin, FlightOption, TripDetails


def award(origin, dest, source, miles, on, aid):
    return AwardResult(
        source=source, origin=origin, destination=dest, date=on, cabin=Cabin.BUSINESS, miles=miles,
        total_taxes=0, currency="USD", remaining_seats=2, airlines="XX", direct=True,
        updated_at="2026-09-19T00:00:00Z", availability_id=aid,
    )


TABLE = {
    ("RNO", "LAX"): [award("RNO", "LAX", "delta", 10_900, JAN9, "RL")],
    ("LAX", "SJO"): [award("LAX", "SJO", "aeroplan", 25_000, JAN9, "LS")],
    ("SJO", "LAX"): [award("SJO", "LAX", "aeroplan", 25_000, JAN16, "SL")],
    ("LAX", "RNO"): [award("LAX", "RNO", "delta", 10_900, JAN16, "LR")],
}


def details(miles, day, dep_hour, arr_hour, seats=2):
    at = lambda h: datetime(day.year, day.month, day.day, h)
    option = FlightOption(
        cabin=Cabin.BUSINESS, miles=miles, seats=seats, departs_at=at(dep_hour), arrives_at=at(arr_hour),
        flight_numbers="XX100", stops=0, total_duration=180, total_taxes=0, currency="USD",
        updated_at="2026-09-19T00:00:00Z",
    )
    return TripDetails(options=(option,), booking_url="https://book.example/x", booking_links=())


DETAILS = {  # each direction lands at LAX at 10:00 and leaves at 14:00: a comfortable 4 hours
    "RL": details(10_900, JAN9, 7, 10), "LS": details(25_000, JAN9, 14, 20),
    "SL": details(25_000, JAN16, 6, 10), "LR": details(10_900, JAN16, 14, 16),
}


class FakeAwards:
    calls_remaining = 900

    def __init__(self):
        self.searched = []

    def search(self, origin, destination, start, end, **_):
        self.searched.append((origin, destination))
        return [r for r in TABLE.get((origin, destination), []) if start <= r.date <= end]

    def trips(self, availability_id):
        return DETAILS[availability_id]


class FakeNtfy:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, message, **kwargs):
        if self.fail:
            raise NtfyError("ntfy returned HTTP 503")
        self.sent.append({"message": message, **kwargs})


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SEATS_AERO_API_KEY", "k")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    for name in ("SERPAPI_API_KEY", "NTFY_TOPIC", "NTFY_SERVER"):
        monkeypatch.delenv(name, raising=False)
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    trip_id = db.add_trip(conn, name="Costa Rica", origin="RNO", destination="SJO",
                          depart_date=JAN9, return_date=JAN16, active=True)
    db.set_gateways(conn, trip_id, ["LAX"])
    db.set_balance(conn, "delta", 30_000)
    db.set_balance(conn, "capital_one", 100_000)
    yield conn, tmp_path
    conn.close()


def run(argv, tmp_path, *, ntfy="default", awards=None):
    awards = awards or FakeAwards()
    ntfy_client = FakeNtfy() if ntfy == "default" else ntfy
    code = main(argv, env_file=None, plans_dir=tmp_path / "plans",
                clients_factory=lambda settings: Clients(awards=awards, cash=None, ntfy=ntfy_client))
    return code, awards, ntfy_client


def alert_rows(conn):
    return conn.execute("SELECT * FROM alert_history").fetchall()


def test_dry_run_prints_the_alert_and_changes_nothing(env, capsys):
    conn, tmp_path = env

    code, _, ntfy = run(["notify", "--dry-run"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "would send for Costa Rica" in out and "Costa Rica: 71,800 miles" in out
    assert ntfy.sent == [] and alert_rows(conn) == [] and not (tmp_path / "plans").exists()


def test_dry_run_works_without_an_ntfy_topic(env):
    _, tmp_path = env

    code, _, _ = run(["notify", "--dry-run"], tmp_path, ntfy=None)

    assert code == 0


def test_notify_sends_records_and_saves_the_plan(env, capsys):
    conn, tmp_path = env

    code, _, ntfy = run(["notify"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and len(ntfy.sent) == 1
    sent = ntfy.sent[0]
    assert sent["title"] == "Costa Rica: 71,800 miles"
    assert sent["priority"] == 4 and "warning" in sent["tags"]  # 2 seats left: act today
    assert "Full steps: python -m flight_optimizer plan 1" in sent["message"]
    assert sent["click"] == "https://book.example/x"
    (row,) = alert_rows(conn)
    plans = list((tmp_path / "plans").glob("1-costa-rica-*.txt"))
    assert len(plans) == 1 and row["details"] == str(plans[0]) and row["miles"] == 71_800
    assert "alert sent (ACT TODAY)" in out and "Plan saved to" in out
    assert "STEP 1. VERIFY" in plans[0].read_text()


def test_a_second_run_does_not_repeat_the_same_alert(env, capsys):
    _, tmp_path = env
    _, _, ntfy = run(["notify"], tmp_path)
    capsys.readouterr()

    code, _, _ = run(["notify"], tmp_path, ntfy=ntfy)

    assert code == 0 and len(ntfy.sent) == 1
    assert "nothing new to alert on" in capsys.readouterr().out


def test_a_failed_send_is_not_recorded_so_the_next_run_retries(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["notify"], tmp_path, ntfy=FakeNtfy(fail=True))

    assert code == 1 and alert_rows(conn) == []
    assert "could not send the alert" in capsys.readouterr().err
    retry_code, _, working = run(["notify"], tmp_path)
    assert retry_code == 0 and len(working.sent) == 1 and len(alert_rows(conn)) == 1


def test_notify_needs_a_topic_unless_it_is_a_dry_run(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["notify"], tmp_path, ntfy=None)

    assert code == 1 and "NTFY_TOPIC is not set" in capsys.readouterr().err


def test_only_active_trips_are_searched_and_alerted_on(env):
    conn, tmp_path = env
    db.add_trip(conn, name="Shelved", origin="XXX", destination="YYY", depart_date=JAN9, active=False)

    _, awards, ntfy = run(["notify"], tmp_path)

    assert not any("XXX" in pair or "YYY" in pair for pair in awards.searched)
    assert [s["title"] for s in ntfy.sent] == ["Costa Rica: 71,800 miles"]


def test_notify_with_no_active_trips_says_so(env, capsys):
    conn, tmp_path = env
    db.set_trip_active(conn, 1, False)

    code, _, ntfy = run(["notify"], tmp_path)

    assert code == 0 and ntfy.sent == [] and "No active trips" in capsys.readouterr().out


def test_done_marks_the_trip_inactive_and_stops_alerts(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["done", "1"], tmp_path)

    assert code == 0 and db.active_trips(conn) == []
    assert "marked inactive" in capsys.readouterr().out
    _, _, ntfy = run(["notify"], tmp_path)
    assert ntfy.sent == []


def test_done_with_an_unknown_trip_is_an_error(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["done", "99"], tmp_path)

    assert code == 1 and "No trip with id 99" in capsys.readouterr().err


def test_plan_shows_the_saved_plan_after_an_alert(env, capsys):
    _, tmp_path = env
    run(["notify"], tmp_path)
    capsys.readouterr()

    code, awards, _ = run(["plan"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and "Plan file:" in out and "STEP 1. VERIFY" in out
    assert awards.searched == []  # showing a saved plan makes no API calls


def test_plan_with_nothing_saved_explains_how_to_make_one(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["plan"], tmp_path)

    assert code == 1 and "plan 1 --refresh" in capsys.readouterr().out


def test_plan_refresh_searches_again_and_saves_a_new_plan(env, capsys):
    _, tmp_path = env

    code, awards, _ = run(["plan", "1", "--refresh"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and awards.searched and "Plan saved to" in out and "STEP 2. TRANSFER" in out
    assert len(list((tmp_path / "plans").glob("*.txt"))) == 1


def test_plan_with_several_active_trips_asks_which(env, capsys):
    conn, tmp_path = env
    db.add_trip(conn, name="Other", origin="RNO", destination="SFO", depart_date=JAN9, active=True)

    code, _, _ = run(["plan"], tmp_path)

    assert code == 1 and "Say which trip" in capsys.readouterr().err


def test_test_notification_sends_a_message(env, capsys):
    _, tmp_path = env

    code, _, ntfy = run(["test-notification"], tmp_path)

    assert code == 0 and "notifications from Flight Optimizer are working" in ntfy.sent[0]["message"]
    assert "Test notification sent" in capsys.readouterr().out


def test_test_notification_without_a_topic_is_an_error(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["test-notification"], tmp_path, ntfy=None)

    assert code == 1 and "NTFY_TOPIC is not set" in capsys.readouterr().err


def test_no_command_still_prints_the_report(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["--top", "2"], tmp_path)

    assert code == 0 and "BEST AFFORDABLE OPTIONS" in capsys.readouterr().out


def test_report_subcommand_accepts_top_too(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["report", "--top", "1"], tmp_path)

    assert code == 0 and "BEST AFFORDABLE OPTIONS" in capsys.readouterr().out
