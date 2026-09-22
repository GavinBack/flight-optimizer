from datetime import date, datetime, timedelta, timezone

import pytest

from builders import JAN9, JAN16
from flight_optimizer import db
from flight_optimizer.__main__ import Clients, main
from flight_optimizer.notify import NtfyError
from flight_optimizer.seats_aero import AwardResult, Cabin, FlightOption, TripDetails


RECENT = (date.today() - timedelta(days=1)).isoformat() + "T00:00:00Z"


def award(origin, dest, source, miles, on, aid):
    return AwardResult(
        source=source, origin=origin, destination=dest, date=on, cabin=Cabin.BUSINESS, miles=miles,
        total_taxes=0, currency="USD", remaining_seats=2, airlines="XX", direct=True,
        updated_at=RECENT, availability_id=aid,
    )


TABLE = {
    ("RNO", "LAX"): [award("RNO", "LAX", "delta", 10_900, JAN9, "RL")],
    ("LAX", "SJO"): [award("LAX", "SJO", "aeroplan", 25_000, JAN9, "LS")],
    ("SJO", "LAX"): [award("SJO", "LAX", "aeroplan", 25_000, JAN16, "SL")],
    ("LAX", "RNO"): [award("LAX", "RNO", "delta", 10_900, JAN16, "LR")],
}


def details(miles, day, dep_hour, arr_hour, seats=2, updated=RECENT):
    at = lambda h: datetime(day.year, day.month, day.day, h)
    option = FlightOption(
        cabin=Cabin.BUSINESS, miles=miles, seats=seats, departs_at=at(dep_hour), arrives_at=at(arr_hour),
        flight_numbers="XX100", stops=0, total_duration=180, total_taxes=0, currency="USD",
        updated_at=updated,
    )
    return TripDetails(options=(option,), booking_url="https://book.example/x", booking_links=())


DETAILS = {  # each direction lands at LAX at 10:00 and leaves at 14:00: a comfortable 4 hours
    "RL": details(10_900, JAN9, 7, 10), "LS": details(25_000, JAN9, 14, 20),
    "SL": details(25_000, JAN16, 6, 10), "LR": details(10_900, JAN16, 14, 16),
}


class FakeAwards:
    calls_remaining = 900

    def __init__(self, table=None, detail_table=None):
        self.searched = []
        self.table = table or TABLE
        self.detail_table = detail_table or DETAILS

    def search(self, origin, destination, start, end, **_):
        self.searched.append((origin, destination))
        return [r for r in self.table.get((origin, destination), []) if start <= r.date <= end]

    def trips(self, availability_id, version=None):
        return self.detail_table[availability_id]


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
    # The first weekly digest is due immediately. These tests are about alerts, so start with it
    # already sent; the digest tests below reset this.
    from flight_optimizer.digest import record_digest_sent
    record_digest_sent(conn, datetime.now())
    yield conn, tmp_path
    conn.close()


def run(argv, tmp_path, *, ntfy="default", awards=None, promos=None, advisor=None):
    awards = awards or FakeAwards()
    ntfy_client = FakeNtfy() if ntfy == "default" else ntfy
    code = main(argv, env_file=None, plans_dir=tmp_path / "plans",
                clients_factory=lambda settings, conn, fresh: Clients(
                    awards=awards, cash=None, ntfy=ntfy_client, promos=promos, advisor=advisor))
    return code, awards, ntfy_client


def alert_rows(conn):
    return conn.execute("SELECT * FROM alert_history").fetchall()


def both_cabins():
    """The same trip on sale in economy (a quarter of the miles) and business, from one set of rows."""
    from dataclasses import replace

    table = {
        pair: rows + [replace(r, cabin=Cabin.ECONOMY, miles=r.miles // 4) for r in rows]
        for pair, rows in TABLE.items()
    }
    detail_table = {}
    for key, d in DETAILS.items():
        business = d.options[0]
        detail_table[key] = replace(
            d, options=(business, replace(business, cabin=Cabin.ECONOMY, miles=business.miles // 4))
        )
    return FakeAwards(table, detail_table)


def test_every_cabin_is_searched_once_and_alerted_on_separately(env, capsys):
    conn, tmp_path = env
    awards = both_cabins()

    code, _, ntfy = run(["notify"], tmp_path, awards=awards)

    assert code == 0
    assert [s["title"] for s in ntfy.sent] == [
        "Costa Rica (economy): 17,950 miles", "Costa Rica (business): 71,800 miles",
    ]
    assert len(awards.searched) == len(set(awards.searched))   # three cabins, no repeated search
    assert len(alert_rows(conn)) == 2
    assert len(list((tmp_path / "plans").glob("*.txt"))) == 2  # one plan per cabin, none overwritten
    assert "Costa Rica (premium economy): nothing new to alert on." in capsys.readouterr().out


def test_running_again_repeats_neither_cabin(env):
    _, tmp_path = env
    run(["notify"], tmp_path, awards=both_cabins())

    _, _, ntfy = run(["notify"], tmp_path, awards=both_cabins())

    assert ntfy.sent == []


def test_plan_can_be_shown_for_one_cabin(env, capsys):
    _, tmp_path = env
    run(["notify"], tmp_path, awards=both_cabins())
    capsys.readouterr()

    run(["plan", "1", "--cabin", "economy"], tmp_path)
    economy = capsys.readouterr().out
    run(["plan", "1", "--cabin", "business"], tmp_path)
    business = capsys.readouterr().out

    assert "(economy)" in economy and "17,950 miles" in economy
    assert "(business)" in business and "71,800 miles" in business


def test_plan_refresh_builds_one_plan_per_cabin_with_an_affordable_option(env, capsys):
    _, tmp_path = env

    run(["plan", "1", "--refresh"], tmp_path, awards=both_cabins())

    out = capsys.readouterr().out
    assert out.count("BOOKING PLAN") == 2
    assert "Costa Rica (premium economy): no affordable option right now" in out


def test_the_report_compares_the_cabins(env, capsys):
    _, tmp_path = env

    run(["report"], tmp_path, awards=both_cabins())

    out = capsys.readouterr().out
    assert "CABINS AT A GLANCE" in out
    for cabin in ("(economy)", "(premium economy)", "(business)"):
        assert cabin in out


def test_dry_run_prints_the_alert_and_changes_nothing(env, capsys):
    conn, tmp_path = env

    code, _, ntfy = run(["notify", "--dry-run"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "would send for Costa Rica" in out and "Costa Rica (business): 71,800 miles" in out
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
    assert sent["title"] == "Costa Rica (business): 71,800 miles"
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
    assert "could not send the award alert" in capsys.readouterr().err
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
    assert [s["title"] for s in ntfy.sent] == ["Costa Rica (business): 71,800 miles"]


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


# --- freshness -------------------------------------------------------------------------------

OLD = "2020-01-01T00:00:00Z"


def stale_awards():
    from dataclasses import replace
    table = {pair: [replace(r, updated_at=OLD) for r in rows] for pair, rows in TABLE.items()}
    stale_details = {
        "RL": details(10_900, JAN9, 7, 10, updated=OLD), "LS": details(25_000, JAN9, 14, 20, updated=OLD),
        "SL": details(25_000, JAN16, 6, 10, updated=OLD), "LR": details(10_900, JAN16, 14, 16, updated=OLD),
    }
    return FakeAwards(table, stale_details)


def test_notify_stays_silent_and_says_why_when_every_option_rests_on_stale_data(env, capsys):
    conn, tmp_path = env

    code, _, ntfy = run(["notify"], tmp_path, awards=stale_awards())

    out = capsys.readouterr().out
    assert code == 0 and ntfy.sent == [] and alert_rows(conn) == []
    assert "Costa Rica (business): affordable options exist, but every one relies on award data older than 7 days" in out
    assert "(business): nothing new to alert on" not in out


def test_the_stale_options_are_still_visible_in_the_report(env, capsys):
    _, tmp_path = env

    run(["report"], tmp_path, awards=stale_awards())

    out = capsys.readouterr().out
    assert "DATA STALE, oldest" in out and "BEST AFFORDABLE OPTIONS" in out


def test_current_data_still_alerts_normally(env):
    _, tmp_path = env

    _, _, ntfy = run(["notify"], tmp_path)

    assert len(ntfy.sent) == 1



# --- buy-miles sales: the promos command and the daily check ---------------------------------------------------------

from datetime import timedelta  # noqa: E402

from flight_optimizer.promos import FEEDS, FeedEntry, PromoSourceError  # noqa: E402

FUTURE = (date.today() + timedelta(days=10)).isoformat()


class FakePromoSource:
    def __init__(self, feeds=None, pages=None, fail=()):
        self.feeds, self.pages, self.fail = feeds or {}, pages or {}, set(fail)
        self.feed_calls = []

    def feed(self, url):
        self.feed_calls.append(url)
        if url in self.fail:
            raise PromoSourceError("boom")
        return self.feeds.get(url, [])

    def page_text(self, url):
        return self.pages[url]


def short_in_delta(conn):
    """Delta short by 7,751 for every option: the near-miss a sale could close."""
    db.set_balance(conn, "delta", 14_049)


def add_delta_sale(tmp_path, *extra):
    return run(["promos", "add", "delta", "--bonus", "50", "--ends", FUTURE, *extra], tmp_path)


def test_promos_add_records_a_sale_and_promos_lists_it(env, capsys):
    _, tmp_path = env

    code, _, _ = add_delta_sale(tmp_path, "--tier", "2000+:50")
    assert code == 0 and "Recorded a 50% bonus sale for Delta SkyMiles" in capsys.readouterr().out

    run(["promos", "--no-fetch"], tmp_path)
    out = capsys.readouterr().out
    assert "Delta SkyMiles" in out and "50% bonus" in out and "2,000+: 50%" in out and FUTURE in out and "[manual]" in out


def test_a_sale_added_without_tiers_is_labelled_a_best_case(env, capsys):
    _, tmp_path = env

    add_delta_sale(tmp_path)

    assert "treated as a best case and will not trigger alerts" in capsys.readouterr().out
    run(["promos", "--no-fetch"], tmp_path)
    assert "headline only" in capsys.readouterr().out


def test_tiers_can_be_ranges_or_open_ended_and_are_kept_in_order(env, capsys):
    _, tmp_path = env
    run(["promos", "add", "aeroplan", "--bonus", "100", "--ends", FUTURE, "--tier", "80000+:100", "--tier", "5000-25000:50"], tmp_path)
    capsys.readouterr()

    run(["promos", "--no-fetch"], tmp_path)

    assert "5,000-25,000: 50% | 80,000+: 100%" in capsys.readouterr().out


@pytest.mark.parametrize("argv, message", [
    (["promos", "add", "hilton", "--bonus", "50", "--ends", FUTURE], "Unknown program"),
    (["promos", "add", "delta", "--bonus", "50", "--ends", "next week"], "should look like 2026-09-30"),
    (["promos", "add", "delta", "--bonus", "500", "--ends", FUTURE], "between 1 and 300"),
    (["promos", "add", "delta", "--bonus", "50", "--ends", FUTURE, "--tier", "lots"], "should look like 5000-25000:50"),
    (["promos", "add", "delta", "--bonus", "50", "--ends", FUTURE, "--tier", "9000-2000:50"], "top is below the bottom"),
    (["promos", "add", "delta", "--bonus", "50", "--ends", FUTURE, "--tier", "5000:50"], "should look like"),
])
def test_bad_sale_input_is_rejected_with_a_clear_message(env, capsys, argv, message):
    _, tmp_path = env

    code, _, _ = run(argv, tmp_path)

    assert code == 1 and message in capsys.readouterr().err
    run(["promos", "--no-fetch"], tmp_path)
    assert "No live buy-miles sales" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["promos", "add", "delta", "--bonus", "50", "--discount", "40", "--ends", FUTURE],   # both kinds
    ["promos", "add", "delta", "--ends", FUTURE],                                          # neither
    ["promos", "add", "delta", "--bonus", "50"],                                            # no end date
])
def test_the_add_command_insists_on_a_kind_and_an_end_date(env, argv):
    _, tmp_path = env

    with pytest.raises(SystemExit) as excinfo:
        run(argv, tmp_path)

    assert excinfo.value.code == 2


def test_removing_only_touches_the_sales_added_by_hand(env, capsys):
    conn, tmp_path = env
    add_delta_sale(tmp_path)
    from flight_optimizer.promos import Promo, save_promo
    save_promo(conn, Promo(key="feed:delta", program="delta", kind="bonus", headline_percent=30, tiers=(),
                           ends=date.today() + timedelta(days=5), title="feed", url="https://x.test", source="Feed", last_seen=date.today()))
    capsys.readouterr()

    code, _, _ = run(["promos", "remove", "delta"], tmp_path)

    assert code == 0 and "Removed 1 hand-added sale(s)" in capsys.readouterr().out
    run(["promos", "--no-fetch"], tmp_path)
    out = capsys.readouterr().out
    assert "[Feed]" in out and "[manual]" not in out


def test_removing_when_nothing_was_added_says_so(env, capsys):
    _, tmp_path = env

    run(["promos", "remove", "delta"], tmp_path)

    assert "No hand-added sales for Delta SkyMiles" in capsys.readouterr().out


def test_the_promos_command_reads_the_feeds_then_lists_what_it_found(env, capsys):
    _, tmp_path = env
    source = FakePromoSource(feeds={FEEDS["LoyaltyLobby"]: [
        FeedEntry("Delta Buy SkyMiles At Up To 50% Bonus Until " + (date.today() + timedelta(days=9)).strftime("%B %d, %Y"),
                  "https://ll.test/delta", date.today())]})

    code, _, _ = run(["promos"], tmp_path, promos=source)

    out = capsys.readouterr().out
    assert code == 0 and source.feed_calls and "1 sale(s) found or refreshed" in out
    assert "Delta SkyMiles" in out and "50% bonus" in out and "[LoyaltyLobby]" in out
    assert "confirm terms on the program's page" in out


def test_no_fetch_never_goes_online(env):
    _, tmp_path = env
    source = FakePromoSource()

    run(["promos", "--no-fetch"], tmp_path, promos=source)

    assert source.feed_calls == []


def test_a_broken_sales_source_is_reported_but_is_not_fatal(env, capsys):
    _, tmp_path = env
    source = FakePromoSource(fail=[FEEDS["Live From A Lounge"]])

    code, _, _ = run(["promos"], tmp_path, promos=source)

    assert code == 0 and "could not read: Live From A Lounge: boom" in capsys.readouterr().err


def test_an_empty_sales_list_says_so(env, capsys):
    _, tmp_path = env

    run(["promos", "--no-fetch"], tmp_path)

    assert "No live buy-miles sales on record." in capsys.readouterr().out


# --- sale alerts in the daily notify ----------------------------------------------------------------------------------

def test_notify_sends_a_sale_alert_when_a_known_tier_sale_closes_a_gap(env, capsys):
    conn, tmp_path = env
    short_in_delta(conn)
    add_delta_sale(tmp_path, "--tier", "2000+:50", "--url", "https://delta.test/sale")
    capsys.readouterr()

    code, _, ntfy = run(["notify"], tmp_path)

    assert code == 0 and len(ntfy.sent) == 1
    sent = ntfy.sent[0]
    assert sent["title"] == "Costa Rica (business): delta sale closes a gap" and sent["click"] == "https://delta.test/sale"
    assert "Total: 71,800 miles + $225.75 (retail would be $301.00)." in sent["message"]
    (row,) = alert_rows(conn)
    assert row["alert_type"] == "promo" and row["cash_price"] is not None
    assert "sale alert sent" in capsys.readouterr().out


def test_the_sale_alert_is_not_repeated_on_the_next_run(env, capsys):
    conn, tmp_path = env
    short_in_delta(conn)
    add_delta_sale(tmp_path, "--tier", "2000+:50")
    _, _, ntfy = run(["notify"], tmp_path)

    run(["notify"], tmp_path, ntfy=ntfy)

    assert len(ntfy.sent) == 1 and len(alert_rows(conn)) == 1


def test_a_headline_only_sale_never_alerts_but_still_shows_in_the_report(env, capsys):
    conn, tmp_path = env
    short_in_delta(conn)
    add_delta_sale(tmp_path)   # no tiers
    capsys.readouterr()

    _, _, ntfy = run(["notify"], tmp_path)
    assert ntfy.sent == []

    run(["report"], tmp_path)
    out = capsys.readouterr().out
    assert "GAPS YOU COULD CLOSE BY BUYING MILES" in out and "BEST CASE: this sale's tiers are not known" in out


def test_the_report_prices_a_gap_with_a_sale_and_shows_the_retail_price(env, capsys):
    conn, tmp_path = env
    short_in_delta(conn)
    add_delta_sale(tmp_path, "--tier", "2000+:50")
    capsys.readouterr()

    run(["report"], tmp_path)

    out = capsys.readouterr().out
    assert "buy 6,000 delta miles for about $225.75" in out and "50% bonus sale until" in out and "retail $301.00" in out


def test_notify_refreshes_the_sales_feeds_once_and_survives_a_broken_one(env, capsys):
    _, tmp_path = env
    source = FakePromoSource(fail=[FEEDS["Doctor of Credit"]])

    code, _, _ = run(["notify", "--dry-run"], tmp_path, promos=source)

    assert code == 0 and len(source.feed_calls) == len(FEEDS)
    assert "could not read a sales source: Doctor of Credit: boom" in capsys.readouterr().err


def test_a_dry_run_shows_the_sale_alert_and_sends_and_records_nothing(env, capsys):
    conn, tmp_path = env
    short_in_delta(conn)
    add_delta_sale(tmp_path, "--tier", "2000+:50")
    capsys.readouterr()

    _, _, ntfy = run(["notify", "--dry-run"], tmp_path)

    out = capsys.readouterr().out
    assert "would send for Costa Rica: sale alert" in out and "delta sale closes a gap" in out
    assert ntfy.sent == [] and alert_rows(conn) == []


def test_a_failed_sale_alert_is_not_recorded_so_it_retries(env, capsys):
    conn, tmp_path = env
    short_in_delta(conn)
    add_delta_sale(tmp_path, "--tier", "2000+:50")
    capsys.readouterr()

    code, _, _ = run(["notify"], tmp_path, ntfy=FakeNtfy(fail=True))

    assert code == 1 and alert_rows(conn) == []
    assert "could not send the sale alert" in capsys.readouterr().err
    _, _, working = run(["notify"], tmp_path)
    assert len(working.sent) == 1


def test_an_expired_sale_never_alerts(env):
    conn, tmp_path = env
    short_in_delta(conn)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    run(["promos", "add", "delta", "--bonus", "50", "--ends", yesterday, "--tier", "2000+:50"], tmp_path)

    _, _, ntfy = run(["notify"], tmp_path)

    assert ntfy.sent == []


def test_the_list_shows_one_entry_per_program_preferring_the_one_with_tiers(env, capsys):
    conn, tmp_path = env
    from flight_optimizer.promos import Promo, PromoTier, save_promo
    end = date.today() + timedelta(days=5)
    save_promo(conn, Promo(key="roundup#aeroplan", program="aeroplan", kind="bonus", headline_percent=100,
                           tiers=(PromoTier(5_000, 25_000, 50),), ends=end, title="r", url="https://r.test", source="Roundup", last_seen=date.today()))
    save_promo(conn, Promo(key="title#aeroplan", program="aeroplan", kind="bonus", headline_percent=100,
                           tiers=(), ends=end, title="t", url="https://t.test", source="Title", last_seen=date.today()))

    run(["promos", "--no-fetch"], tmp_path)

    out = capsys.readouterr().out
    assert out.count("Air Canada Aeroplan") == 1 and "5,000-25,000: 50%" in out and "headline only" not in out



# --- the weekly digest ---------------------------------------------------------------------------------------------------

from flight_optimizer.digest import digest_due  # noqa: E402


def digests(ntfy):
    return [m for m in ntfy.sent if m["title"].startswith("Weekly flight check")]


def make_digest_due(conn):
    conn.execute("DELETE FROM meta WHERE key = 'digest:last_sent'")
    conn.commit()


def test_notify_sends_the_first_digest_and_remembers_it(env, capsys):
    conn, tmp_path = env
    make_digest_due(conn)

    code, _, ntfy = run(["notify"], tmp_path)

    assert code == 0 and len(digests(ntfy)) == 1
    sent = digests(ntfy)[0]
    assert sent["title"] == "Weekly flight check: Costa Rica" and sent["priority"] == 2 and sent["tags"] == ("calendar",)
    assert "Full report: python -m flight_optimizer report" in sent["message"]
    assert not digest_due(conn, datetime.now()) and "Weekly digest sent." in capsys.readouterr().out


def test_the_digest_is_not_repeated_within_a_week(env):
    conn, tmp_path = env
    make_digest_due(conn)
    _, _, ntfy = run(["notify"], tmp_path)

    run(["notify"], tmp_path, ntfy=ntfy)

    assert len(digests(ntfy)) == 1


def test_it_comes_round_again_after_seven_days(env):
    conn, tmp_path = env
    make_digest_due(conn)
    _, _, ntfy = run(["notify"], tmp_path)
    eight_days_ago = (datetime.now() - timedelta(days=8)).astimezone().isoformat()
    db.set_meta(conn, "digest:last_sent", eight_days_ago)

    run(["notify"], tmp_path, ntfy=ntfy)

    assert len(digests(ntfy)) == 2


def test_the_digest_follows_the_alerts_and_counts_them(env):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify"], tmp_path)

    titles = [m["title"] for m in ntfy.sent]
    assert titles[0] == "Costa Rica (business): 71,800 miles" and titles[-1].startswith("Weekly flight check")
    assert "Alerts sent in the last 7 days: 1 award." in digests(ntfy)[0]["message"]


def test_a_dry_run_shows_the_digest_but_neither_sends_it_nor_starts_the_timer(env, capsys):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify", "--dry-run"], tmp_path)

    out = capsys.readouterr().out
    assert "would send for all trips: weekly digest" in out and "Weekly flight check: Costa Rica" in out
    assert ntfy.sent == [] and digest_due(conn, datetime.now())


def test_a_failed_digest_is_not_recorded_so_it_is_retried(env, capsys):
    conn, tmp_path = env
    make_digest_due(conn)

    code, _, _ = run(["notify"], tmp_path, ntfy=FakeNtfy(fail=True))

    assert code == 1 and digest_due(conn, datetime.now())
    assert "could not send the weekly digest" in capsys.readouterr().err
    _, _, working = run(["notify"], tmp_path)
    assert len(digests(working)) == 1


def test_no_digest_without_active_trips(env):
    conn, tmp_path = env
    make_digest_due(conn)
    db.set_trip_active(conn, 1, False)

    _, _, ntfy = run(["notify"], tmp_path)

    assert ntfy.sent == []


def test_the_digest_says_when_the_only_options_are_stale(env):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify"], tmp_path, awards=stale_awards())

    message = digests(ntfy)[0]["message"]
    assert "1 affordable award option: 0 current, 0 aging, 1 stale." in message
    assert "That is silence, not an absence of options." in message
    assert [m for m in ntfy.sent if not m["title"].startswith("Weekly")] == []   # no award alert, as designed


def test_the_digest_never_contains_balance_amounts(env):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify"], tmp_path)

    message = digests(ntfy)[0]["message"]
    for amount in ("30,000", "100,000", "30000", "100000"):
        assert amount not in message


def test_the_digest_command_sends_now_even_if_one_went_out_today(env, capsys):
    conn, tmp_path = env    # the fixture already recorded a digest as sent just now

    code, _, ntfy = run(["digest"], tmp_path)

    assert code == 0 and len(ntfy.sent) == 1 and ntfy.sent[0]["title"].startswith("Weekly flight check")
    assert "Weekly digest sent." in capsys.readouterr().out


def test_the_digest_command_dry_run_prints_without_sending_or_resetting_the_timer(env, capsys):
    conn, tmp_path = env
    make_digest_due(conn)

    code, _, ntfy = run(["digest", "--dry-run"], tmp_path)

    assert code == 0 and ntfy.sent == [] and digest_due(conn, datetime.now())
    assert "Weekly flight check: Costa Rica" in capsys.readouterr().out


def test_the_digest_command_without_a_topic_is_an_error_unless_a_dry_run(env, capsys):
    _, tmp_path = env

    assert run(["digest"], tmp_path, ntfy=None)[0] == 1
    assert "NTFY_TOPIC is not set" in capsys.readouterr().err
    assert run(["digest", "--dry-run"], tmp_path, ntfy=None)[0] == 0


def test_the_digest_command_with_no_active_trips_says_so(env, capsys):
    conn, tmp_path = env
    db.set_trip_active(conn, 1, False)

    code, _, ntfy = run(["digest"], tmp_path)

    assert code == 0 and ntfy.sent == [] and "nothing to summarize" in capsys.readouterr().out


def test_a_failed_digest_command_exits_nonzero_and_does_not_reset_the_timer(env):
    conn, tmp_path = env
    make_digest_due(conn)

    code, _, _ = run(["digest"], tmp_path, ntfy=FakeNtfy(fail=True))

    assert code == 1 and digest_due(conn, datetime.now())



# --- the trip command -----------------------------------------------------------------------------------------------------

def trip_rows(conn):
    return conn.execute("SELECT * FROM trips ORDER BY id").fetchall()


def test_trip_with_no_action_lists_the_trips(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["trip"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and "Costa Rica" in out and "RNO > SJO" in out and "LAX" in out


def test_trip_add_saves_it_and_says_what_it_will_cost_to_search(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["trip", "add", "Kelowna", "RNO", "YLW", "--depart", "2026-12-22", "--return", "2026-12-28",
                      "--gateways", "SEA", "LAX", "PHX"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and "Added trip 2: Kelowna, RNO > YLW, Dec 22 2026 - Dec 28 2026, priced in economy, premium economy and business." in out
    assert "Gateways: LAX, PHX, SEA" in out and "Up to 14 Seats.aero searches per run" in out
    assert "Evaluate it with: python -m flight_optimizer report" in out
    assert [t["active"] for t in trip_rows(conn)] == [1, 1] and db.get_gateways(conn, 2) == ["LAX", "PHX", "SEA"]


def test_a_trip_added_without_gateways_warns_that_only_the_direct_pair_is_searched(env, capsys):
    _, tmp_path = env

    run(["trip", "add", "Kelowna", "RNO", "YLW", "--depart", "2026-12-22", "--return", "2026-12-28"], tmp_path)

    out = capsys.readouterr().out
    assert "No gateways: only the direct RNO > YLW pair is searched" in out and "gateways 2 --add HUB" in out


def test_a_one_way_or_inactive_trip(env, capsys):
    conn, tmp_path = env

    run(["trip", "add", "Later", "RNO", "YLW", "--depart", "2027-03-01", "--inactive"], tmp_path)

    out = capsys.readouterr().out
    assert "(one way), priced in" in out and "Saved inactive. Start searching it with: python -m flight_optimizer trip set 2 --active" in out
    assert trip_rows(conn)[1]["active"] == 0


@pytest.mark.parametrize("argv, message", [
    (["trip", "add", "X", "RNO", "YLW", "--depart", "soon"], "should look like 2026-12-22"),
    (["trip", "add", "X", "RNO", "RNO", "--depart", "2026-12-22"], "same airport"),
    (["trip", "add", "X", "RNO", "YLW", "--depart", "2026-12-22", "--return", "2026-12-01"], "return date is before"),
    (["trip", "add", "X", "RNO", "YLW", "--depart", "2026-12-22", "--gateways", "RNO"], "origin or destination"),
    (["trip", "add", "Again", "RNO", "SJO", "--depart", "2027-01-09", "--return", "2027-01-16"], "already exists (trip 1)"),
])
def test_bad_trip_input_is_rejected_and_nothing_is_saved(env, capsys, argv, message):
    conn, tmp_path = env

    code, _, _ = run(argv, tmp_path)

    assert code == 1 and message in capsys.readouterr().err
    assert len(trip_rows(conn)) == 1


def test_trip_add_needs_a_departure_date_and_has_no_cabin_option(env):
    _, tmp_path = env

    # Every trip is priced in every cabin, so there is nothing to choose.
    for argv in (["trip", "add", "X", "RNO", "YLW"], ["trip", "add", "X", "RNO", "YLW", "--depart", "2026-12-22", "--cabin", "economy"]):
        with pytest.raises(SystemExit) as excinfo:
            run(argv, tmp_path)
        assert excinfo.value.code == 2


def test_trip_set_changes_and_shows_the_result(env, capsys):
    conn, tmp_path = env

    code, _, _ = run(["trip", "set", "1", "--name", "Costa Rica 2027"], tmp_path)

    out = capsys.readouterr().out
    assert code == 0 and "Updated." in out and "Costa Rica 2027" in out
    assert trip_rows(conn)[0]["name"] == "Costa Rica 2027"


def test_trip_set_can_pause_and_resume_a_trip(env):
    conn, tmp_path = env

    run(["trip", "set", "1", "--inactive"], tmp_path)
    assert db.active_trips(conn) == []
    run(["trip", "set", "1", "--active"], tmp_path)
    assert len(db.active_trips(conn)) == 1


def test_trip_set_can_drop_the_return_leg(env):
    conn, tmp_path = env

    run(["trip", "set", "1", "--one-way"], tmp_path)

    assert trip_rows(conn)[0]["return_date"] is None


def test_trip_set_with_nothing_to_change_says_so(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["trip", "set", "1"], tmp_path)

    assert code == 1 and "Nothing to change" in capsys.readouterr().out


def test_trip_set_errors_are_clear(env, capsys):
    conn, tmp_path = env

    assert run(["trip", "set", "99", "--name", "x"], tmp_path)[0] == 1
    assert "No trip with id 99" in capsys.readouterr().err
    assert run(["trip", "set", "1", "--depart", "2027-06-01"], tmp_path)[0] == 1     # the return is Jan 16 2027
    assert "return date is before" in capsys.readouterr().err
    assert trip_rows(conn)[0]["depart_date"] == "2027-01-09"


def test_active_and_inactive_cannot_both_be_given(env):
    _, tmp_path = env

    with pytest.raises(SystemExit) as excinfo:
        run(["trip", "set", "1", "--active", "--inactive"], tmp_path)

    assert excinfo.value.code == 2


def test_trip_commands_need_no_api_clients(env):
    _, tmp_path = env

    def exploding_factory(settings, conn, fresh):
        raise AssertionError("local commands must not build API clients")

    assert main(["trip"], env_file=None, plans_dir=tmp_path / "plans", clients_factory=exploding_factory) == 0


# --- the Claude advisor ---------------------------------------------------------------------------------------

class FakeAdvisor:
    model = "claude-sonnet-5"

    def __init__(self, text="RECOMMENDATION: take economy.", truncated=False, error=None):
        self.text, self.truncated, self.error, self.briefs = text, truncated, error, []

    def advise(self, brief, **_):
        from flight_optimizer.advisor import Advice
        self.briefs.append(brief)
        if self.error:
            raise self.error
        return Advice(text=self.text, model=self.model, input_tokens=4000, output_tokens=500, truncated=self.truncated)


def test_advise_sends_the_computed_report_and_prints_the_answer(env, capsys):
    _, tmp_path = env
    advisor = FakeAdvisor()

    code, _, _ = run(["advise"], tmp_path, awards=both_cabins(), advisor=advisor)

    out = capsys.readouterr().out
    assert code == 0 and "RECOMMENDATION: take economy." in out
    assert "Asking Claude (claude-sonnet-5) about 1 trip" in out and "4,000 tokens in, 500 out" in out
    (brief,) = advisor.briefs
    assert "CABINS AT A GLANCE" in brief and "17,950 miles" in brief and "71,800 miles" in brief
    assert "Delta SkyMiles 30,000" in brief          # balances are part of what is sent


def test_advise_covers_every_active_trip_in_one_call(env):
    conn, tmp_path = env
    db.add_trip(conn, name="Kelowna", origin="RNO", destination="YLW", depart_date=JAN9, return_date=JAN16, active=True)
    advisor = FakeAdvisor()

    run(["advise"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 1
    assert "Costa Rica" in advisor.briefs[0] and "Kelowna" in advisor.briefs[0]


def test_advise_can_be_limited_to_one_trip(env):
    conn, tmp_path = env
    db.add_trip(conn, name="Kelowna", origin="RNO", destination="YLW", depart_date=JAN9, return_date=JAN16, active=True)
    advisor = FakeAdvisor()

    run(["advise", "2"], tmp_path, advisor=advisor)

    assert "Kelowna" in advisor.briefs[0] and "Costa Rica" not in advisor.briefs[0]


def test_advise_dry_run_shows_everything_that_would_be_sent_and_sends_nothing(env, capsys):
    _, tmp_path = env
    advisor = FakeAdvisor()

    code, _, _ = run(["advise", "--dry-run"], tmp_path, awards=both_cabins(), advisor=advisor)

    out = capsys.readouterr().out
    assert code == 0 and advisor.briefs == []
    assert "SYSTEM PROMPT" in out and "MESSAGE" in out and "Balances (miles or points)" in out
    assert "nothing was sent" in out and "input tokens" in out


def test_advise_dry_run_needs_no_api_key(env):
    _, tmp_path = env

    assert run(["advise", "--dry-run"], tmp_path, advisor=None)[0] == 0


def test_advise_without_a_key_says_how_to_set_one_and_searches_nothing(env, capsys):
    _, tmp_path = env

    code, awards, _ = run(["advise"], tmp_path, advisor=None)

    assert code == 1 and "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert awards.searched == []


def test_advise_reports_an_api_failure_cleanly(env, capsys):
    from flight_optimizer.advisor import AdvisorError
    _, tmp_path = env

    code, _, _ = run(["advise"], tmp_path, advisor=FakeAdvisor(error=AdvisorError("Claude returned HTTP 429: rate limited")))

    assert code == 1 and "HTTP 429" in capsys.readouterr().err


def test_advise_says_when_the_answer_was_cut_off(env, capsys):
    _, tmp_path = env

    run(["advise"], tmp_path, advisor=FakeAdvisor(truncated=True))

    assert "cut off at the length limit" in capsys.readouterr().out


def test_advise_with_no_active_trips_does_nothing(env, capsys):
    conn, tmp_path = env
    db.set_trip_active(conn, 1, False)
    advisor = FakeAdvisor()

    code, _, _ = run(["advise"], tmp_path, advisor=advisor)

    assert code == 0 and advisor.briefs == [] and "nothing to advise on" in capsys.readouterr().out


# --- advice after each notify run ------------------------------------------------------------------------------

def test_notify_asks_claude_after_the_alerts_and_saves_the_answer(env, capsys):
    _, tmp_path = env
    advisor = FakeAdvisor(text="RECOMMENDATION: economy.")
    awards = both_cabins()

    code, _, ntfy = run(["notify"], tmp_path, awards=awards, advisor=advisor)

    out = capsys.readouterr().out
    assert code == 0 and len(ntfy.sent) == 2                       # the alerts went out as usual
    (brief,) = advisor.briefs
    assert "CABINS AT A GLANCE" in brief and "17,950 miles" in brief
    (saved,) = (tmp_path / "advice").glob("advice-*.txt")
    assert "RECOMMENDATION: economy." in saved.read_text()
    assert "Advice saved to" in out and "advise --last" in out


def test_the_advice_never_goes_to_the_phone(env):
    _, tmp_path = env

    _, _, ntfy = run(["notify"], tmp_path, advisor=FakeAdvisor(text="Use your 100,000 Capital One miles"))

    assert all("Capital One" not in sent["message"] for sent in ntfy.sent)


def test_notify_needs_no_extra_searches_for_the_advice(env):
    _, tmp_path = env
    without, with_advice = FakeAwards(), FakeAwards()

    run(["notify", "--no-advice"], tmp_path, awards=without, advisor=FakeAdvisor())
    run(["notify"], tmp_path, awards=with_advice, advisor=FakeAdvisor())

    assert len(with_advice.searched) == len(without.searched)


def test_no_advice_skips_the_claude_call(env):
    _, tmp_path = env
    advisor = FakeAdvisor()

    run(["notify", "--no-advice"], tmp_path, advisor=advisor)

    assert advisor.briefs == [] and not (tmp_path / "advice").exists()


def test_a_dry_run_does_not_spend_a_claude_call(env, capsys):
    _, tmp_path = env
    advisor = FakeAdvisor()

    run(["notify", "--dry-run"], tmp_path, advisor=advisor)

    assert advisor.briefs == [] and "Advice: would ask Claude" in capsys.readouterr().out


def test_without_a_key_notify_says_advice_was_skipped_and_still_succeeds(env, capsys):
    _, tmp_path = env

    code, _, ntfy = run(["notify"], tmp_path, advisor=None)

    assert code == 0 and len(ntfy.sent) == 1
    assert "Advice: skipped, ANTHROPIC_API_KEY is not set." in capsys.readouterr().out


def test_a_claude_failure_never_fails_the_run_or_loses_the_alerts(env, capsys):
    from flight_optimizer.advisor import AdvisorError
    conn, tmp_path = env

    code, _, ntfy = run(["notify"], tmp_path, advisor=FakeAdvisor(error=AdvisorError("Claude returned HTTP 429: limit")))

    assert code == 0 and len(alert_rows(conn)) == 1     # the run still succeeded, and the award alert went out
    assert [m["priority"] for m in ntfy.sent] == [4, 2]  # plus a low-priority note that the advice is degraded
    assert "Claude advice: Claude returned HTTP 429: limit" in ntfy.sent[1]["message"]
    assert "could not get advice: Claude returned HTTP 429" in capsys.readouterr().err


def test_nothing_is_asked_when_there_are_no_active_trips(env):
    conn, tmp_path = env
    db.set_trip_active(conn, 1, False)
    advisor = FakeAdvisor()

    run(["notify"], tmp_path, advisor=advisor)

    assert advisor.briefs == []


def test_advise_saves_its_answer_and_last_shows_it_without_calling_claude(env, capsys):
    _, tmp_path = env
    run(["advise"], tmp_path, advisor=FakeAdvisor(text="RECOMMENDATION: wait."))
    capsys.readouterr()
    advisor = FakeAdvisor()

    code, _, _ = run(["advise", "--last"], tmp_path, advisor=advisor)

    out = capsys.readouterr().out
    assert code == 0 and "RECOMMENDATION: wait." in out and "Advice file:" in out
    assert advisor.briefs == []


def test_advise_last_with_nothing_saved_says_how_to_make_some(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["advise", "--last"], tmp_path)

    assert code == 1 and "No saved advice yet" in capsys.readouterr().out


# --- skipping advice when nothing changed --------------------------------------------------------------------------

def test_a_second_run_with_nothing_changed_does_not_ask_again(env, capsys):
    _, tmp_path = env
    advisor = FakeAdvisor()
    run(["notify"], tmp_path, advisor=advisor)
    capsys.readouterr()

    run(["notify"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 1
    out = capsys.readouterr().out
    assert "Advice: skipped, nothing has changed since the last answer" in out and "advise --last" in out


def test_a_changed_balance_asks_again(env):
    conn, tmp_path = env
    advisor = FakeAdvisor()
    run(["notify"], tmp_path, advisor=advisor)

    db.set_balance(conn, "delta", 31_000)
    run(["notify"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 2


def test_new_award_availability_asks_again(env):
    _, tmp_path = env
    advisor = FakeAdvisor()
    run(["notify"], tmp_path, awards=FakeAwards(), advisor=advisor)

    run(["notify"], tmp_path, awards=both_cabins(), advisor=advisor)

    assert len(advisor.briefs) == 2


def test_it_asks_again_if_the_saved_answer_is_gone(env):
    _, tmp_path = env
    advisor = FakeAdvisor()
    run(["notify"], tmp_path, advisor=advisor)
    for saved in (tmp_path / "advice").glob("*.txt"):
        saved.unlink()

    run(["notify"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 2


def test_a_failed_call_is_retried_next_run(env):
    from flight_optimizer.advisor import AdvisorError
    _, tmp_path = env
    run(["notify"], tmp_path, advisor=FakeAdvisor(error=AdvisorError("HTTP 529")))
    advisor = FakeAdvisor()

    run(["notify"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 1     # nothing was recorded as answered, so it is asked


def test_a_dry_run_says_whether_it_would_ask_or_skip(env, capsys):
    _, tmp_path = env
    advisor = FakeAdvisor()
    run(["notify"], tmp_path, advisor=advisor)
    capsys.readouterr()

    run(["notify", "--dry-run"], tmp_path, advisor=advisor)

    assert "Advice: would be skipped, nothing has changed" in capsys.readouterr().out
    assert len(advisor.briefs) == 1


def test_advise_on_every_trip_means_notify_need_not_ask_again(env):
    _, tmp_path = env
    advisor = FakeAdvisor()
    run(["advise"], tmp_path, advisor=advisor)

    run(["notify"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 1


def test_advise_on_one_trip_does_not_stand_in_for_the_full_check(env):
    conn, tmp_path = env
    db.add_trip(conn, name="Kelowna", origin="RNO", destination="YLW", depart_date=JAN9, return_date=JAN16, active=True)
    advisor = FakeAdvisor()
    run(["advise", "1"], tmp_path, advisor=advisor)

    run(["notify"], tmp_path, advisor=advisor)

    assert len(advisor.briefs) == 2


# --- the digest points at the advice ---------------------------------------------------------------------------------

def test_the_weekly_digest_points_at_todays_advice_without_quoting_it(env):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify"], tmp_path, advisor=FakeAdvisor(text="Use your 100,000 Capital One miles"))

    (digest,) = digests(ntfy)
    assert f"Latest advice ({datetime.now():%b %d}): python -m flight_optimizer advise --last" in digest["message"]
    assert "100,000" not in digest["message"] and "Capital One miles" not in digest["message"]


def test_the_digest_has_no_advice_line_when_there_is_none(env):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify"], tmp_path, advisor=None)

    assert "Latest advice" not in digests(ntfy)[0]["message"]


def test_the_digest_command_points_at_the_latest_saved_advice(env):
    conn, tmp_path = env
    run(["advise"], tmp_path, advisor=FakeAdvisor())
    make_digest_due(conn)

    _, _, ntfy = run(["digest"], tmp_path)

    assert "Latest advice (" in digests(ntfy)[0]["message"]


# --- flexible travel windows -------------------------------------------------------------------------------------------

def shifted_awards():
    """The same trip, but the space is on Jan 7 and Jan 14: two days before and two before the asked dates."""
    jan7, jan14 = date(2027, 1, 7), date(2027, 1, 14)
    table = {
        ("RNO", "LAX"): [award("RNO", "LAX", "delta", 10_900, jan7, "RL")],
        ("LAX", "SJO"): [award("LAX", "SJO", "aeroplan", 25_000, jan7, "LS")],
        ("SJO", "LAX"): [award("SJO", "LAX", "aeroplan", 25_000, jan14, "SL")],
        ("LAX", "RNO"): [award("LAX", "RNO", "delta", 10_900, jan14, "LR")],
    }
    detail_table = {
        "RL": details(10_900, jan7, 7, 10), "LS": details(25_000, jan7, 14, 20),
        "SL": details(25_000, jan14, 6, 10), "LR": details(10_900, jan14, 14, 16),
    }
    return FakeAwards(table, detail_table)


def test_an_exact_trip_finds_nothing_when_the_space_is_on_other_days(env):
    _, tmp_path = env

    _, _, ntfy = run(["notify"], tmp_path, awards=shifted_awards())

    assert ntfy.sent == []


def test_a_flexible_trip_finds_it_and_the_alert_says_which_days(env):
    conn, tmp_path = env
    run(["trip", "set", "1", "--flexible"], tmp_path)

    _, _, ntfy = run(["notify"], tmp_path, awards=shifted_awards())

    (sent,) = ntfy.sent
    assert sent["title"] == "Costa Rica (business): 71,800 miles"
    assert sent["message"].splitlines()[0] == "RNO > SJO  Jan 07 - Jan 14 (you asked Jan 09 - Jan 16)  (business)"
    (plan,) = (tmp_path / "plans").glob("*business*.txt")
    assert "you asked Jan 09 2027 - Jan 16 2027, flexible" in plan.read_text()


def test_a_flexible_report_lists_the_shifted_dates(env, capsys):
    _, tmp_path = env
    run(["trip", "set", "1", "--flexible"], tmp_path)

    run(["report"], tmp_path, awards=shifted_awards())

    out = capsys.readouterr().out
    assert "(flexible: arrive up to 3 days early, return +/-3)" in out and "Jan 07 - Jan 14" in out


def test_trip_add_can_be_flexible_and_says_so(env, capsys):
    conn, tmp_path = env

    run(["trip", "add", "Kelowna", "RNO", "YLW", "--depart", "2026-12-22", "--return", "2026-12-28", "--flexible"], tmp_path)

    out = capsys.readouterr().out
    assert "Flexible: awards may land up to 3 days before the arrival date (never after it)" in out
    assert trip_rows(conn)[1]["flexible"] == 1


def test_trips_are_exact_by_default(env, capsys):
    conn, tmp_path = env

    run(["trip", "add", "Kelowna", "RNO", "YLW", "--depart", "2026-12-22"], tmp_path)

    assert "Exact dates." in capsys.readouterr().out and trip_rows(conn)[1]["flexible"] == 0


def test_trip_set_switches_between_flexible_and_exact(env):
    conn, tmp_path = env

    run(["trip", "set", "1", "--flexible"], tmp_path)
    assert trip_rows(conn)[0]["flexible"] == 1
    run(["trip", "set", "1", "--exact"], tmp_path)
    assert trip_rows(conn)[0]["flexible"] == 0


def test_flexible_and_exact_cannot_both_be_given(env):
    _, tmp_path = env

    with pytest.raises(SystemExit) as excinfo:
        run(["trip", "set", "1", "--flexible", "--exact"], tmp_path)

    assert excinfo.value.code == 2


def test_the_trip_list_shows_which_trips_are_flexible(env, capsys):
    _, tmp_path = env
    run(["trip", "set", "1", "--flexible"], tmp_path)
    capsys.readouterr()

    run(["trip", "list"], tmp_path)

    lines = capsys.readouterr().out.splitlines()
    assert "Flexible" in lines[0] and "-3 / ±3" in lines[1]


def test_switching_to_exact_alone_counts_as_a_change(env, capsys):
    _, tmp_path = env

    code, _, _ = run(["trip", "set", "1", "--exact"], tmp_path)

    assert code == 0 and "Updated." in capsys.readouterr().out


# --- failure alerts ---------------------------------------------------------------------------------------------------

class ExplodingAwards(FakeAwards):
    def __init__(self, error, only=None):
        super().__init__()
        self.error, self.only = error, only

    def search(self, origin, destination, start, end, **kw):
        if self.only is None or (origin, destination) == self.only:
            raise self.error
        return super().search(origin, destination, start, end, **kw)


def failures(ntfy):
    return [m for m in ntfy.sent if m["title"].startswith("Flight check")]


def test_a_search_failure_alerts_the_phone_and_fails_the_run(env, capsys):
    from flight_optimizer.seats_aero import SeatsAeroRateLimitError
    _, tmp_path = env

    code, _, ntfy = run(["notify"], tmp_path, awards=ExplodingAwards(SeatsAeroRateLimitError("Daily Seats.aero call quota exhausted", 429)))

    (alert,) = failures(ntfy)
    assert code == 1 and alert["title"] == "Flight check failed" and alert["priority"] == 4
    assert "- Costa Rica: Daily Seats.aero call quota exhausted" in alert["message"] and "logs/notify.log" in alert["message"]
    assert "error: Costa Rica: Daily Seats.aero call quota exhausted" in capsys.readouterr().err


def test_an_unexpected_error_is_named_by_type_only_but_logged_in_full(env, capsys):
    _, tmp_path = env
    secret = ValueError("boom with x-api-key: sk-secret-value")

    code, _, ntfy = run(["notify"], tmp_path, awards=ExplodingAwards(secret))

    (alert,) = failures(ntfy)
    assert code == 1 and "unexpected ValueError" in alert["message"]
    assert "sk-secret-value" not in alert["message"] and "sk-secret-value" not in alert["title"]
    assert "Traceback" in capsys.readouterr().err        # the local log has the detail


def test_one_trips_failure_does_not_cost_the_other_trip_its_alerts(env):
    conn, tmp_path = env
    trip_id = db.add_trip(conn, name="Kelowna", origin="RNO", destination="YLW", depart_date=JAN9, return_date=JAN16, active=True)
    # Costa Rica's searches work; Kelowna's (RNO>YLW) fail.
    awards = ExplodingAwards(RuntimeError("x"), only=("RNO", "YLW"))

    code, _, ntfy = run(["notify"], tmp_path, awards=awards)

    assert code == 1
    assert any(m["title"].startswith("Costa Rica") for m in ntfy.sent)          # its award alert still went out
    assert "- Kelowna: unexpected RuntimeError" in failures(ntfy)[0]["message"]


def test_the_same_failure_is_not_repeated_within_a_day_but_is_the_next_day(env):
    conn, tmp_path = env
    boom = ExplodingAwards(RuntimeError("x"))
    run(["notify"], tmp_path, awards=boom)

    _, _, again = run(["notify"], tmp_path, awards=boom)
    assert failures(again) == []

    from flight_optimizer import failures as f
    key = next(k for k in ("failure:" + f.build_failure_alert([f.Problem("Costa Rica", "unexpected RuntimeError")]).key,))
    db.set_meta(conn, key, (datetime.now(timezone.utc) - timedelta(hours=21)).isoformat())
    _, _, next_day = run(["notify"], tmp_path, awards=boom)
    assert len(failures(next_day)) == 1


def test_a_failed_search_does_not_send_an_empty_digest(env):
    conn, tmp_path = env
    make_digest_due(conn)

    _, _, ntfy = run(["notify"], tmp_path, awards=ExplodingAwards(RuntimeError("x")))

    assert digests(ntfy) == []


def test_a_healthy_run_sends_no_failure_alert(env):
    _, tmp_path = env

    _, _, ntfy = run(["notify"], tmp_path)

    assert failures(ntfy) == []


def test_a_dry_run_shows_the_failure_alert_but_records_nothing(env, capsys):
    conn, tmp_path = env

    code, _, ntfy = run(["notify", "--dry-run"], tmp_path, awards=ExplodingAwards(RuntimeError("x")))

    assert code == 1 and ntfy.sent == [] and "would send for Flight Optimizer: failure alert" in capsys.readouterr().out
    assert db.get_meta(conn, "notify:last_attempt") is None


def test_broken_cash_prices_are_reported_as_degraded_but_a_missing_key_is_not(env):
    from flight_optimizer.cash_fares import SerpApiQuotaError
    _, tmp_path = env

    class BrokenCash:
        searches_made = 0
        def search(self, *a, **k):
            raise SerpApiQuotaError("SerpApi search quota exhausted")

    ntfy = FakeNtfy()
    code = main(["notify"], env_file=None, plans_dir=tmp_path / "plans",
                clients_factory=lambda s, c, f: Clients(awards=FakeAwards(), cash=BrokenCash(), ntfy=ntfy))
    (alert,) = failures(ntfy)
    assert code == 0 and alert["title"] == "Flight check: something is degraded" and alert["priority"] == 2
    assert "Cash prices for Costa Rica: SerpApi search quota exhausted" in alert["message"]

    _, _, no_key = run(["notify"], tmp_path)   # cash=None: no key set
    assert failures(no_key) == []


def test_an_error_outside_the_per_trip_work_still_alerts(env, monkeypatch):
    import flight_optimizer.__main__ as cli
    _, tmp_path = env
    monkeypatch.setattr(cli, "active_promos", lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))

    code, _, ntfy = run(["notify"], tmp_path)

    (alert,) = failures(ntfy)
    assert code == 1 and "- Daily check: unexpected KeyError" in alert["message"]


# --- a missed run ---------------------------------------------------------------------------------------------------------

def test_the_first_ever_run_is_not_a_missed_run(env):
    _, tmp_path = env

    _, _, ntfy = run(["notify"], tmp_path)

    assert not any(m["title"] == "Flight check was missed" for m in ntfy.sent)


def test_a_run_after_a_long_gap_says_it_was_missed(env):
    conn, tmp_path = env
    db.set_meta(conn, "notify:last_attempt", (datetime.now(timezone.utc) - timedelta(days=3, hours=1)).isoformat())

    _, _, ntfy = run(["notify"], tmp_path)

    (missed,) = [m for m in ntfy.sent if m["title"] == "Flight check was missed"]
    assert "No daily check ran for about 3 days" in missed["message"] and missed["priority"] == 3


def test_daily_runs_are_never_missed_runs(env):
    conn, tmp_path = env
    run(["notify"], tmp_path)
    db.set_meta(conn, "notify:last_attempt", (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())

    _, _, ntfy = run(["notify"], tmp_path)

    assert not any(m["title"] == "Flight check was missed" for m in ntfy.sent)


def test_a_run_that_failed_still_counts_as_attempted_so_it_is_not_reported_as_missed(env):
    conn, tmp_path = env
    run(["notify"], tmp_path, awards=ExplodingAwards(RuntimeError("x")))
    assert db.get_meta(conn, "notify:last_attempt") is not None

    _, _, ntfy = run(["notify"], tmp_path)

    assert not any(m["title"] == "Flight check was missed" for m in ntfy.sent)


# --- trips that share balances ---------------------------------------------------------------------------------------

def add_twin_trip(conn):
    """A second trip over the same route and dates: its best option needs exactly what the first trip's does."""
    trip_id = db.add_trip(conn, name="Kelowna", origin="RNO", destination="SJO", depart_date=JAN9, return_date=JAN16, active=True)
    db.set_gateways(conn, trip_id, ["LAX"])
    return trip_id


def test_each_alert_says_what_it_leaves_the_other_trip(env):
    conn, tmp_path = env
    add_twin_trip(conn)     # both need 21,800 Delta miles and 50,000 through Capital One; Delta has 30,000

    _, _, ntfy = run(["notify"], tmp_path)

    alerts = [m for m in ntfy.sent if m["title"].endswith("71,800 miles")]
    assert {m["title"] for m in alerts} == {"Costa Rica (business): 71,800 miles", "Kelowna (business): 71,800 miles"}
    by_trip = {m["title"].split(" ")[0]: m["message"] for m in alerts}
    assert "Shared miles: with this booked, nothing affordable is left for Kelowna." in by_trip["Costa"]
    assert "Shared miles: with this booked, nothing affordable is left for Costa Rica." in by_trip["Kelowna"]
    assert all("115" not in m["message"] and "30,000" not in m["message"] for m in alerts)   # no balances


def test_the_plan_carries_the_same_warning(env):
    conn, tmp_path = env
    add_twin_trip(conn)

    run(["notify"], tmp_path)

    plan = next(p for p in (tmp_path / "plans").glob("1-*business*.txt"))
    assert "OTHER TRIPS SHARE THESE MILES" in plan.read_text()


def test_a_single_active_trip_gets_no_shared_miles_note(env):
    _, tmp_path = env

    _, _, ntfy = run(["notify"], tmp_path)

    assert all("Shared miles" not in m["message"] for m in ntfy.sent)


def test_trips_that_fit_together_get_no_warning(env):
    conn, tmp_path = env
    add_twin_trip(conn)
    db.set_balance(conn, "delta", 60_000)
    db.set_balance(conn, "capital_one", 200_000)      # room for both

    _, _, ntfy = run(["notify"], tmp_path)

    assert all("Shared miles" not in m["message"] for m in ntfy.sent)


def test_the_report_has_a_shared_balances_section_for_several_trips(env, capsys):
    conn, tmp_path = env
    add_twin_trip(conn)

    run(["report"], tmp_path)

    out = capsys.readouterr().out
    assert "SHARED BALANCES" in out and "Both by award: NOT possible" in out


def test_the_report_for_one_trip_has_no_such_section(env, capsys):
    _, tmp_path = env

    run(["report"], tmp_path)

    assert "SHARED BALANCES" not in capsys.readouterr().out


def test_a_bug_in_the_joint_check_costs_no_alerts(env, monkeypatch):
    import flight_optimizer.__main__ as cli
    conn, tmp_path = env
    add_twin_trip(conn)
    monkeypatch.setattr(cli, "build_joint", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))

    code, _, ntfy = run(["notify"], tmp_path)

    assert code == 0                                                 # an extra failing is not a failed run
    assert len([m for m in ntfy.sent if m["title"].endswith("71,800 miles")]) == 2
    assert any("Shared-balance check" in m["message"] for m in ntfy.sent)     # but it is reported, at low priority


# --- cash-price alerts ------------------------------------------------------------------------------------------------------

class PricedCash:
    """Cash fares by cabin, each direction half the round trip."""

    def __init__(self, prices):
        self.prices, self.searches_made = prices, 0

    def search(self, origin, destination, on_date, cabin, **_):
        from flight_optimizer.cash_fares import CashFare, CashSearch
        self.searches_made += 1
        half = self.prices[cabin] // 2
        fare_ = CashFare(price=half, total_duration=600, layovers=(120,), airlines=("United",), flight_numbers=("UA 1",), cabins=("x",))
        return CashSearch(fares=(fare_,), lowest_price=None, typical_range=None,
                          fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def run_cash(argv, tmp_path, prices, *, awards=None):
    ntfy = FakeNtfy()
    awards = awards or FakeAwards()
    code = main(argv, env_file=None, plans_dir=tmp_path / "plans",
                clients_factory=lambda s, c, f: Clients(awards=awards, cash=PricedCash(prices), ntfy=ntfy))
    return code, ntfy


PRICES = {Cabin.ECONOMY: 700, Cabin.PREMIUM_ECONOMY: 1_200, Cabin.BUSINESS: 2_400}


def seed_history(conn, cabin, totals):
    for days, total in zip(range(len(totals) + 1, 1, -1), totals):
        db.record_cash_total(conn, 1, cabin, (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"), total)


def cash_alerts(ntfy):
    return [m for m in ntfy.sent if "cash fare" in m["title"]]


def test_an_economy_drop_alerts_the_phone(env):
    conn, tmp_path = env
    seed_history(conn, "economy", [820, 800, 810, 830])

    code, ntfy = run_cash(["notify"], tmp_path, PRICES)

    (alert,) = cash_alerts(ntfy)
    assert code == 0 and alert["title"] == "Costa Rica: economy cash fare dropped: $700"
    assert "Typical lately: $815 (median of 4 searches" in alert["message"]
    (row,) = [r for r in alert_rows(conn) if r["alert_type"] == "cash"]
    assert row["cash_price"] == 700


def test_a_run_with_no_notable_price_is_quiet_and_still_builds_history(env):
    conn, tmp_path = env

    _, ntfy = run_cash(["notify"], tmp_path, PRICES)

    assert cash_alerts(ntfy) == []
    assert {r["cabin"] for r in conn.execute("SELECT cabin FROM cash_history")} == {"economy", "premium_economy", "business"}


def test_the_same_cash_search_is_not_history_twice_across_runs(env):
    conn, tmp_path = env
    run_cash(["notify"], tmp_path, PRICES)
    run_cash(["notify"], tmp_path, PRICES)      # the second run reads the cached search: same fetch time

    assert conn.execute("SELECT COUNT(*) FROM cash_history WHERE cabin = 'economy'").fetchone()[0] == 1


def test_a_target_alerts_in_any_cabin_and_is_not_repeated(env):
    conn, tmp_path = env
    db.set_cash_target(conn, 1, "business", 2_500)

    _, first = run_cash(["notify"], tmp_path, PRICES)
    _, second = run_cash(["notify"], tmp_path, PRICES)

    (alert,) = cash_alerts(first)
    assert alert["title"] == "Costa Rica: business cash fare at your $2,500 target: $2,400"
    assert cash_alerts(second) == []


def test_a_trip_that_is_not_active_gets_no_cash_alerts(env):
    conn, tmp_path = env
    db.set_cash_target(conn, 1, "economy", 900)
    db.set_trip_active(conn, 1, False)

    _, ntfy = run_cash(["notify"], tmp_path, PRICES)

    assert ntfy.sent == []


def test_a_dry_run_shows_the_cash_alert_but_sends_and_records_no_alert(env, capsys):
    conn, tmp_path = env
    db.set_cash_target(conn, 1, "economy", 900)

    _, ntfy = run_cash(["notify", "--dry-run"], tmp_path, PRICES)

    assert ntfy.sent == [] and "cash-fare alert" in capsys.readouterr().out
    assert [r for r in alert_rows(conn) if r["alert_type"] == "cash"] == []


def test_the_digest_counts_cash_alerts(env):
    conn, tmp_path = env
    db.set_cash_target(conn, 1, "economy", 900)
    make_digest_due(conn)

    _, ntfy = run_cash(["notify"], tmp_path, PRICES)

    line = next(l for l in digests(ntfy)[0]["message"].splitlines() if l.startswith("Alerts sent"))
    assert line == "Alerts sent in the last 7 days: 1 award, 1 cash-fare."


# --- the trip target command --------------------------------------------------------------------------------------------------

def test_target_can_be_set_shown_and_cleared(env, capsys):
    conn, tmp_path = env

    run(["trip", "target", "1", "economy", "700"], tmp_path)
    assert "you will be alerted when the economy cash fare is at or under $700 round trip" in capsys.readouterr().out
    assert db.get_cash_targets(conn, 1) == {"economy": 700}

    run(["trip", "target", "1"], tmp_path)
    assert "Costa Rica: economy at or under $700 round trip" in capsys.readouterr().out

    run(["trip", "target", "1", "economy", "--clear"], tmp_path)
    assert "economy target cleared." in capsys.readouterr().out and db.get_cash_targets(conn, 1) == {}


def test_target_with_nothing_set_says_how(env, capsys):
    _, tmp_path = env

    run(["trip", "target", "1"], tmp_path)

    out = capsys.readouterr().out
    assert "no cash targets" in out and "trip target 1 economy 700" in out


@pytest.mark.parametrize("argv, message", [
    (["trip", "target", "1", "economy"], "Give an amount in dollars, or --clear."),
    (["trip", "target", "1", "economy", "0"], "more than zero"),
    (["trip", "target", "1", "economy", "-5"], "more than zero"),
    (["trip", "target", "99", "economy", "700"], "No trip with id 99"),
])
def test_bad_target_input_is_refused(env, capsys, argv, message):
    conn, tmp_path = env

    code, _, _ = run(argv, tmp_path)

    assert code == 1 and message in capsys.readouterr().err and db.get_cash_targets(conn, 1) == {}


def test_target_only_accepts_a_real_cabin(env):
    _, tmp_path = env

    with pytest.raises(SystemExit) as excinfo:
        run(["trip", "target", "1", "first", "900"], tmp_path)

    assert excinfo.value.code == 2


def test_clearing_a_target_that_was_never_set_says_so(env, capsys):
    _, tmp_path = env

    run(["trip", "target", "1", "economy", "--clear"], tmp_path)

    assert "was not set" in capsys.readouterr().out
