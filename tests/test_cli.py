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

    def trips(self, availability_id, version=None):
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
                clients_factory=lambda settings, conn, fresh: Clients(awards=awards, cash=None, ntfy=ntfy_client))
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


def run_recording_fresh(argv, tmp_path):
    seen = []

    def factory(settings, conn, fresh):
        seen.append(fresh)
        return Clients(awards=FakeAwards(), cash=None, ntfy=FakeNtfy())

    main(argv, env_file=None, plans_dir=tmp_path / "plans", clients_factory=factory)
    return seen


def test_searches_may_come_from_cache_unless_fresh_is_asked_for(env):
    _, tmp_path = env

    assert run_recording_fresh(["report"], tmp_path) == [False]
    assert run_recording_fresh(["--fresh", "report"], tmp_path) == [True]
    assert run_recording_fresh(["--fresh", "notify", "--dry-run"], tmp_path) == [True]


def test_a_plan_refresh_always_searches_fresh(env):
    _, tmp_path = env

    assert run_recording_fresh(["plan", "1", "--refresh"], tmp_path) == [True]
    assert run_recording_fresh(["plan", "1"], tmp_path) == [False]


def test_the_default_clients_use_the_caching_wrapper(env):
    from flight_optimizer.__main__ import default_clients
    from flight_optimizer.config import load_settings
    from flight_optimizer.seats_cache import CachedSeatsAero

    conn, _ = env

    normal = default_clients(load_settings(env_file=None), conn)
    fresh = default_clients(load_settings(env_file=None), conn, fresh=True)

    assert isinstance(normal.awards, CachedSeatsAero) and isinstance(fresh.awards, CachedSeatsAero)
    assert normal.awards._use_search_cache is True and fresh.awards._use_search_cache is False


# --- balance command -------------------------------------------------------------------

def balances_in_db(conn):
    return db.get_balances(conn)


def test_balance_with_no_arguments_lists_every_program(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["balance"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and "Delta SkyMiles" in out and "30,000" in out and "Capital One Miles" in out
    assert out.count("\n") >= 13


def test_balance_sets_one_program_and_shows_the_change(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["balance", "delta", "16000"], tmp_path)

    assert code == 0 and balances_in_db(conn)["delta"] == 16_000
    out = capsys.readouterr().out
    assert "Delta SkyMiles: 30,000 -> 16,000" in out and "next check will use the new balances" in out


def test_balance_accepts_friendly_names_commas_and_several_at_once(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["balance", "Qantas Frequent Flyer", "12,000", "aa", "26115", "capital-one", "115,519"], tmp_path)

    balances = balances_in_db(conn)
    assert code == 0 and (balances["qantas"], balances["american"], balances["capital_one"]) == (12_000, 26_115, 115_519)


def test_setting_the_same_value_says_unchanged(env, capsys):
    _, tmp_path = env

    run(["balance", "delta", "30000"], tmp_path)

    assert "Delta SkyMiles: unchanged at 30,000" in capsys.readouterr().out


def test_a_balance_can_be_set_to_zero(env):
    conn, tmp_path = env

    code, _, _ = run(["balance", "delta", "0"], tmp_path)

    assert code == 0 and balances_in_db(conn)["delta"] == 0


def test_a_bad_pair_changes_nothing_at_all(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["balance", "delta", "99999", "hilton", "5"], tmp_path)

    assert code == 1 and balances_in_db(conn)["delta"] == 30_000  # the valid one was not applied either
    assert "Unknown program 'hilton'" in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    (["balance", "delta"], "pairs"),
    (["balance", "delta", "-5"], "whole number of miles"),
    (["balance", "delta", "1.5"], "whole number of miles"),
    (["balance", "delta", "100", "delta", "200"], "given twice"),
    (["balance", "a", "5"], "could mean any of"),
])
def test_invalid_balance_input_is_rejected_with_a_clear_message(env, capsys, argv, message):
    conn, tmp_path = env

    code, _, _ = run(argv, tmp_path)

    assert code == 1 and message in capsys.readouterr().err
    assert balances_in_db(conn)["delta"] == 30_000


def test_balance_commands_need_no_api_clients(env):
    _, tmp_path = env

    def exploding_factory(settings, conn, fresh):
        raise AssertionError("local commands must not build API clients")

    assert main(["balance"], env_file=None, plans_dir=tmp_path / "plans", clients_factory=exploding_factory) == 0
    assert main(["gateways"], env_file=None, plans_dir=tmp_path / "plans", clients_factory=exploding_factory) == 0


def test_a_new_balance_changes_what_the_next_check_finds(env, capsys):
    _, tmp_path = env
    run(["notify", "--dry-run"], tmp_path)
    assert "would send" in capsys.readouterr().out

    run(["balance", "delta", "0"], tmp_path)  # Delta cannot be topped up from Capital One
    capsys.readouterr()
    code, _, _ = run(["notify", "--dry-run"], tmp_path)

    assert code == 0 and "nothing new to alert on" in capsys.readouterr().out


# --- gateways command -------------------------------------------------------------------

def test_gateways_shows_the_list_and_what_a_run_costs(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["gateways"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and "Costa Rica (trip 1): RNO > SJO" in out and "Gateways: LAX" in out
    assert "Up to 6 Seats.aero searches per run" in out  # 2 directions x (1 direct + 2 for one gateway)


def test_gateways_add_remove_and_set(env, capsys):
    conn, tmp_path = env

    run(["gateways", "--add", "den", "sfo"], tmp_path)
    assert db.get_gateways(conn, 1) == ["DEN", "LAX", "SFO"]
    assert "Up to 14 Seats.aero searches" in capsys.readouterr().out

    run(["gateways", "--remove", "DEN"], tmp_path)
    assert db.get_gateways(conn, 1) == ["LAX", "SFO"]

    run(["gateways", "1", "--set", "dfw", "LAX", "dfw"], tmp_path)
    assert db.get_gateways(conn, 1) == ["DFW", "LAX"]  # normalized and de-duplicated
    assert "Gateways updated" in capsys.readouterr().out


def test_adding_an_existing_gateway_is_a_no_op(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["gateways", "--add", "LAX"], tmp_path)

    assert code == 0 and db.get_gateways(conn, 1) == ["LAX"]
    assert "No change" in capsys.readouterr().out


def test_removing_a_gateway_that_is_not_there_is_an_error_and_changes_nothing(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["gateways", "--remove", "LAX", "DEN"], tmp_path)

    assert code == 1 and db.get_gateways(conn, 1) == ["LAX"]  # LAX was NOT removed
    err = capsys.readouterr().err
    assert "DEN is not a gateway" in err and "Nothing was changed" in err


def test_removing_every_gateway_warns_that_only_the_direct_pair_is_searched(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["gateways", "--remove", "LAX"], tmp_path)

    assert code == 0 and db.get_gateways(conn, 1) == []
    out = capsys.readouterr().out
    assert "Gateways: none" in out and "only the direct RNO > SJO pair is searched" in out
    assert "Up to 2 Seats.aero searches" in out


@pytest.mark.parametrize("argv, message", [
    (["gateways", "--set", "LOS ANGELES"], "3-letter airport code"),
    (["gateways", "--add", "RNO"], "origin or destination"),
    (["gateways", "--add", "SJO"], "origin or destination"),
    (["gateways", "--set", "LAX", "12"], "3-letter airport code"),
])
def test_invalid_gateways_are_rejected_and_the_list_is_untouched(env, capsys, argv, message):
    conn, tmp_path = env

    code, _, _ = run(argv, tmp_path)

    assert code == 1 and message in capsys.readouterr().err
    assert db.get_gateways(conn, 1) == ["LAX"]


def test_gateways_for_a_one_way_trip_cost_half_as_much(env, capsys):
    conn, tmp_path = env
    db.set_trip_active(conn, 1, False)
    db.add_trip(conn, name="One way", origin="RNO", destination="SFO", depart_date=JAN9, active=True)

    run(["gateways", "2", "--add", "LAX", "DEN"], tmp_path)

    assert "Up to 5 Seats.aero searches per run" in capsys.readouterr().out  # 1 direction x (1 + 2*2)


def test_gateways_needs_a_trip_id_when_several_are_active(env, capsys):
    conn, tmp_path = env
    db.add_trip(conn, name="Other", origin="RNO", destination="SFO", depart_date=JAN9, active=True)

    code, _, _ = run(["gateways"], tmp_path)

    assert code == 1 and "Say which trip" in capsys.readouterr().err


def test_gateways_for_an_unknown_trip_is_an_error(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["gateways", "99"], tmp_path)

    assert code == 1 and "No trip with id 99" in capsys.readouterr().err


def test_gateways_options_are_mutually_exclusive(env):
    _, tmp_path = env

    with pytest.raises(SystemExit) as excinfo:
        run(["gateways", "--add", "DEN", "--remove", "LAX"], tmp_path)

    assert excinfo.value.code == 2
