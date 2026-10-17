"""Daily reconciliation of the ledger against vendor billing fixtures."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from plate_daemon.ledger import Ledger
from plate_daemon.reconcile import (
    BillingFormatError,
    BillingRow,
    load_billing,
    reconcile,
    record_billed,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "billing"
DAY = "2026-10-16"


def t(hms: str, day: str = DAY) -> str:
    return f"{day}T{hms}Z"


def submit(
    led: Ledger,
    run_id: str,
    engine: str,
    quoted: float,
    when: str,
    *,
    grade: str = "C1",
    job: str | None = None,
    model: str | None = None,
    end: str = "failed",
    day: str = DAY,
) -> None:
    led.append(
        kind="quoted",
        run_id=run_id,
        engine_id=engine,
        custody_grade=grade,
        cost_usd_quoted=quoted,
        at=t(when, day),
    )
    led.append(kind="approved", run_id=run_id, cost_usd_quoted=quoted, by="human", at=t(when, day))
    led.append(
        kind="submitted",
        run_id=run_id,
        provider_job_id=job,
        data={"model": model} if model else None,
        at=t(when, day),
    )
    led.append(kind=end, run_id=run_id, at=t(when, day))


@pytest.fixture
def led(tmp_path: Path) -> Ledger:
    """The day's runs: three matched by provider job id, three by (model, minute, amount), one failed job
    the vendor did not bill, one run cancelled before it was submitted."""
    led = Ledger(tmp_path / "proj")
    submit(led, "o1", "gpt-image-2.5", 0.19, "09:12:25", job="resp_7f3a91", model="gpt-image-2.5", end="done")
    submit(led, "o2", "gpt-image-2.5", 0.19, "09:40:10", model="gpt-image-2.5", end="done")
    submit(led, "g1", "nano-banana-2.1", 0.067, "10:02:05", model="gemini-3.1-flash-image", end="done")
    submit(led, "g2", "nano-banana-pro", 0.134, "10:15:40", model="gemini-3-pro-image", end="done")
    submit(led, "a1", "seedance-2.5-i2v", 1.20, "11:00:00", grade="C2", job="pred_a1b2c3", end="done")
    submit(led, "a2", "kling-o3-4k", 2.50, "11:20:30", grade="C2", job="pred_d4e5f6", end="done")
    submit(led, "f1", "seedance-2.5-r2v", 0.84, "13:00:00", grade="C2", job="pred_fail01", end="failed")
    led.append(kind="quoted", run_id="c1", engine_id="kling-o3-4k", cost_usd_quoted=2.5, at=t("15:00:00"))
    led.append(kind="cancelled", run_id="c1", at=t("15:01:00"))
    return led


# ---------------------------------------------------------------------------- the fixtures


def test_clean_day_reconciles(led: Ledger):
    rep = reconcile(led, load_billing(FIXTURES / "clean"), DAY)
    assert rep.ok, rep.failures
    assert rep.unmatched_billing == [] and rep.unledgered_spend_usd == 0.0
    assert {m["run_id"]: m["matched_by"] for m in rep.matched} == {
        "o1": "provider_job_id",
        "a1": "provider_job_id",
        "a2": "provider_job_id",
        "o2": "model+minute+amount",
        "g1": "model+minute+amount",
        "g2": "model+minute+amount",
    }
    assert rep.by_engine["gpt-image-2.5"] == {"quoted": 0.38, "billed": 0.38, "delta_pct": 0.0, "rows": 2}
    assert rep.by_engine["nano-banana-pro"]["delta_pct"] == pytest.approx(0.7463, abs=1e-4)
    assert rep.by_engine["kling-o3-4k"] == {"quoted": 2.5, "billed": 2.52, "delta_pct": 0.8, "rows": 1}
    assert set(rep.by_engine) == {
        "gpt-image-2.5",
        "nano-banana-2.1",
        "nano-banana-pro",
        "seedance-2.5-i2v",
        "kling-o3-4k",
    }


def test_unbilled_failed_job_is_listed_but_does_not_fail_the_day(led: Ledger):
    rep = reconcile(led, load_billing(FIXTURES / "clean"), DAY)
    assert [u["run_id"] for u in rep.unbilled_runs] == ["f1"]  # c1 never left the building
    assert rep.unbilled_runs[0]["quoted_usd"] == 0.84 and rep.ok


def test_one_unmatched_row_fails_the_day_with_its_amount(led: Ledger):
    rep = reconcile(led, load_billing(FIXTURES / "unmatched"), DAY)
    assert rep.ok is False
    assert [r.provider_job_id for r in rep.unmatched_billing] == ["pred_zzz999"]
    assert rep.unmatched_billing[0].provider == "atlas"
    assert rep.unledgered_spend_usd == 0.84
    assert any("unledgered spend USD 0.8400" in f for f in rep.failures)
    # the matched part of the day is unchanged
    assert rep.by_engine == reconcile(led, load_billing(FIXTURES / "clean"), DAY).by_engine


def test_the_day_filter_uses_the_billing_row_time_in_utc(led: Ledger):
    rows = load_billing(FIXTURES / "clean")
    prev = reconcile(led, rows, "2026-10-15")
    assert prev.unledgered_spend_usd == 0.19 and [r.provider_job_id for r in prev.unmatched_billing] == [
        "resp_prev_day"
    ]
    assert reconcile(led, rows, date(2026, 10, 16)).ok
    assert reconcile(led, rows, datetime(2026, 10, 16, 23, 59, tzinfo=UTC)).ok
    nothing = reconcile(led, rows, "2026-10-20")
    assert nothing.ok and nothing.by_engine == {} and nothing.unbilled_runs == []


def test_report_is_plain_json(led: Ledger):
    rep = reconcile(led, load_billing(FIXTURES / "unmatched"), DAY)
    d = json.loads(json.dumps(rep.to_dict()))
    assert d["ok"] is False and d["unmatched_billing"][0]["provider_job_id"] == "pred_zzz999"
    assert d["by_engine"]["kling-o3-4k"]["billed"] == 2.52


def test_reconcile_accepts_a_plain_record_list(led: Ledger):
    assert reconcile(led.records(), load_billing(FIXTURES / "clean"), DAY).ok


# ---------------------------------------------------------------------------- price drift


def one_run(tmp_path: Path, quoted: float = 100.0) -> Ledger:
    led = Ledger(tmp_path / "one")
    submit(led, "r1", "kling-o3-4k", quoted, "11:00:00", grade="C2", job="job-1", end="done")
    return led


def row(
    usd: float, job: str | None = "job-1", model: str = "kwaivgi/kling-o3-4k", hms: str = "11:00:30"
) -> BillingRow:
    return BillingRow("atlas", datetime.fromisoformat(t(hms)), job, model, usd)


@pytest.mark.parametrize(
    "billed, ok",
    [(100.0, True), (101.0, True), (99.0, True), (101.01, False), (98.99, False), (110.0, False)],
)
def test_one_percent_tolerance_is_inclusive(tmp_path: Path, billed: float, ok: bool):
    rep = reconcile(one_run(tmp_path), [row(billed)], DAY)
    assert rep.ok is ok
    assert rep.unmatched_billing == []  # matched by id either way: drift is a delta, not a leak
    if not ok:
        assert "kling-o3-4k" in rep.failures[0]


def test_tolerance_is_a_parameter(tmp_path: Path):
    assert reconcile(one_run(tmp_path), [row(104.0)], DAY, tolerance_pct=5).ok
    assert not reconcile(one_run(tmp_path), [row(104.0)], DAY, tolerance_pct=1).ok


def test_several_rows_for_one_job_sum_and_the_quote_counts_once(tmp_path: Path):
    rep = reconcile(one_run(tmp_path), [row(60.0), row(40.5, hms="11:03:00")], DAY)
    assert rep.by_engine["kling-o3-4k"] == {"quoted": 100.0, "billed": 100.5, "delta_pct": 0.5, "rows": 2}
    assert len(rep.matched) == 1 and rep.matched[0]["rows"] == 2 and rep.ok


def test_billed_with_nothing_quoted_is_undefined_not_ok(tmp_path: Path):
    led = Ledger(tmp_path / "free")
    led.append(kind="submitted", run_id="r1", engine_id="local", provider_job_id="j", at=t("09:00:00"))
    rep = reconcile(led, [row(0.5, job="j")], DAY)
    assert rep.by_engine["local"]["delta_pct"] is None and not rep.ok


# ---------------------------------------------------------------------------- the fallback match


def fuzzy_run(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "fz")
    submit(
        led, "r1", "nano-banana-2.1", 0.50, "10:00:30", model="gemini-3.1-flash-image", job=None, end="done"
    )
    return led


def frow(
    usd: float = 0.50, hms: str = "10:00:50", model: str = "gemini-3.1-flash-image", job: str | None = None
) -> BillingRow:
    return BillingRow("google", datetime.fromisoformat(t(hms)), job, model, usd)


@pytest.mark.parametrize(
    "kw, matched",
    [
        ({}, True),
        ({"hms": "10:00:05"}, True),  # same minute, before the submit
        ({"hms": "10:01:10"}, True),  # next minute bucket: inside the default one-minute slack
        ({"hms": "10:03:00"}, False),
        ({"hms": "09:58:00"}, False),
        ({"usd": 0.51}, True),  # exactly one cent away
        ({"usd": 0.49}, True),
        ({"usd": 0.52}, False),
        ({"model": "gemini-3-pro-image"}, False),
        ({"model": "Gemini_3.1-Flash-Image"}, True),  # case and punctuation are ignored
        ({"model": "google/gemini-3.1-flash-image"}, True),  # vendor prefix is dropped
        ({"job": "never-recorded-id"}, True),  # the run recorded no id, so a named job is no contradiction
    ],
)
def test_fuzzy_match_conditions(tmp_path: Path, kw: dict, matched: bool):
    led = fuzzy_run(tmp_path)
    rep = reconcile(led, [frow(**kw)], DAY)
    got = [m["run_id"] for m in rep.matched]
    assert (got == ["r1"]) is matched
    assert (rep.unledgered_spend_usd == 0.0) is matched


def test_a_row_naming_another_job_does_not_take_a_run_with_a_different_id(tmp_path: Path):
    led = Ledger(tmp_path / "fz2")
    submit(
        led, "r1", "nano-banana-2.1", 0.50, "10:00:30", model="gemini-3.1-flash-image", job="mine", end="done"
    )
    rep = reconcile(led, [frow(job="theirs")], DAY)
    assert rep.matched == [] and rep.unledgered_spend_usd == 0.5
    assert [m["run_id"] for m in reconcile(led, [frow(job=None)], DAY).matched] == ["r1"]


def test_slack_zero_is_the_strict_minute_bucket(tmp_path: Path):
    led = fuzzy_run(tmp_path)
    assert reconcile(led, [frow(hms="10:01:10")], DAY, bucket_slack_min=0).matched == []
    assert reconcile(led, [frow(hms="10:00:59")], DAY, bucket_slack_min=0).matched != []


def test_pairing_is_one_to_one(tmp_path: Path):
    led = fuzzy_run(tmp_path)
    rep = reconcile(led, [frow(hms="10:00:50"), frow(hms="10:00:55")], DAY)
    assert len(rep.matched) == 1 and len(rep.unmatched_billing) == 1 and rep.unledgered_spend_usd == 0.5
    submit(led, "r2", "nano-banana-2.1", 0.50, "10:00:40", model="gemini-3.1-flash-image", end="done")
    both = reconcile(led, [frow(hms="10:00:50"), frow(hms="10:00:55")], DAY)
    assert sorted(m["run_id"] for m in both.matched) == ["r1", "r2"] and both.ok


def test_nearest_in_time_pairs_first(tmp_path: Path):
    led = fuzzy_run(tmp_path)
    submit(led, "r2", "nano-banana-2.1", 0.50, "10:01:00", model="gemini-3.1-flash-image", end="done")
    early = BillingRow("google", datetime.fromisoformat(t("10:00:31")), None, "gemini-3.1-flash-image", 0.50)
    rep = reconcile(led, [early], DAY)
    assert [m["run_id"] for m in rep.matched] == ["r1"]


def test_model_map_translates_vendor_model_names(tmp_path: Path):
    led = Ledger(tmp_path / "mm")
    submit(led, "r1", "nano-banana-2.1", 0.50, "10:00:30", end="done")  # no model string recorded
    r = frow(model="gemini-3.1-flash-image-preview")
    assert reconcile(led, [r], DAY).matched == []
    rep = reconcile(led, [r], DAY, model_map={"Gemini-3.1-Flash-Image-Preview": "nano-banana-2.1"})
    assert [m["run_id"] for m in rep.matched] == ["r1"]


def test_run_never_submitted_cannot_take_a_fuzzy_row(tmp_path: Path):
    led = Ledger(tmp_path / "ns")
    led.append(
        kind="approved", run_id="r1", engine_id="nano-banana-2.1", cost_usd_quoted=0.5, at=t("10:00:30")
    )
    assert reconcile(led, [frow()], DAY).unledgered_spend_usd == 0.5


def test_zero_dollar_and_credit_rows_do_not_fail_the_day(led: Ledger):
    rows = load_billing(FIXTURES / "clean") + [
        BillingRow("openai", datetime.fromisoformat(t("12:00:00")), None, "gpt-image-2.5", 0.0),
        BillingRow(
            "atlas", datetime.fromisoformat(t("12:05:00")), "pred_refund", "kwaivgi/kling-o3-4k", -0.5
        ),
    ]
    rep = reconcile(led, rows, DAY)
    assert rep.ok and rep.unledgered_spend_usd == 0.0 and rep.credits_usd == -0.5
    assert len(rep.unmatched_billing) == 2  # still listed for a human


def test_unledgered_credit_cannot_cancel_unledgered_spend(led: Ledger):
    extra = [
        BillingRow("atlas", datetime.fromisoformat(t("12:00:00")), "x1", "m", 1.0),
        BillingRow("atlas", datetime.fromisoformat(t("12:01:00")), "x2", "m", -1.0),
    ]
    rep = reconcile(led, load_billing(FIXTURES / "clean") + extra, DAY)
    assert not rep.ok and rep.unledgered_spend_usd == 1.0 and rep.credits_usd == -1.0


# ---------------------------------------------------------------------------- recording the bills


def test_record_billed_writes_each_bill_once_and_feeds_summarize(led: Ledger):
    rep = reconcile(led, load_billing(FIXTURES / "unmatched"), DAY)
    assert record_billed(led, rep) == 6 + 1
    assert record_billed(led, rep) == 0
    assert led.verify() == (True, None)
    s = led.summarize()
    assert s["spent_usd_billed"] == pytest.approx(0.19 + 0.19 + 0.067 + 0.135 + 1.20 + 2.52 + 0.84)
    assert s["by_engine"]["kling-o3-4k"]["spent_usd_billed"] == 2.52
    assert s["by_engine"]["unattributed"]["spent_usd_billed"] == 0.84
    again = reconcile(led, load_billing(FIXTURES / "unmatched"), DAY)  # the new records change nothing
    assert again.by_engine == rep.by_engine and again.unledgered_spend_usd == 0.84
    assert record_billed(led, again) == 0


def test_record_billed_on_another_day_adds_a_second_set(tmp_path: Path):
    led = one_run(tmp_path)
    assert record_billed(led, reconcile(led, [row(100.0)], DAY)) == 1
    later = BillingRow("atlas", datetime.fromisoformat(t("01:00:00", "2026-10-17")), "job-1", "m", 3.0)
    assert record_billed(led, reconcile(led, [later], "2026-10-17")) == 1
    assert led.summarize()["spent_usd_billed"] == 103.0


# ---------------------------------------------------------------------------- reading billing CSVs


def test_load_billing_reads_the_fixture_columns():
    rows = load_billing(FIXTURES / "clean" / "atlas.csv")
    assert rows == [
        BillingRow(
            "atlas",
            datetime(2026, 10, 16, 11, 0, 2, tzinfo=UTC),
            "pred_a1b2c3",
            "bytedance/seedance-2.5-i2v",
            1.2,
        ),
        BillingRow(
            "atlas",
            datetime(2026, 10, 16, 11, 20, 44, tzinfo=UTC),
            "pred_d4e5f6",
            "kwaivgi/kling-o3-4k",
            2.52,
        ),
    ]
    openai = load_billing(FIXTURES / "clean" / "openai.csv")
    assert [r.provider for r in openai] == ["openai"] * 3 and openai[2].provider_job_id is None


def test_load_billing_folder_reads_the_three_provider_files():
    rows = load_billing(FIXTURES / "clean")
    assert [r.provider for r in rows] == ["openai"] * 3 + ["google"] * 2 + ["atlas"] * 2


def test_load_billing_folder_without_any_export_is_an_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_billing(tmp_path)


def test_load_billing_is_forgiving_about_excel_and_windows(tmp_path: Path):
    p = tmp_path / "google.csv"
    p.write_bytes(
        "﻿At,Provider_Job_Id,Model,USD\r\n"
        '2026-10-16 10:00:00,,gemini-3-pro-image,"$1,234.50"\r\n'
        "\r\n"
        "2026-10-16T15:30:00+05:30,job-9,gemini-3-pro-image,0.5\r\n".encode()
    )
    rows = load_billing(p)
    assert [(r.at, r.provider_job_id, r.usd) for r in rows] == [
        (datetime(2026, 10, 16, 10, 0, tzinfo=UTC), None, 1234.5),
        (datetime(2026, 10, 16, 10, 0, tzinfo=UTC), "job-9", 0.5),
    ]


def test_load_billing_maps_vendor_headers(tmp_path: Path):
    p = tmp_path / "atlas.csv"
    p.write_text(
        "created_at,prediction_id,model_name,cost\n2026-10-16T11:00:00Z,pred_1,m,1.5\n",
        encoding="utf-8",
        newline="\n",
    )
    rows = load_billing(
        p,
        columns={
            "at": "created_at",
            "provider_job_id": "prediction_id",
            "model": "model_name",
            "usd": "cost",
        },
    )
    assert rows[0].provider_job_id == "pred_1" and rows[0].usd == 1.5
    with pytest.raises(BillingFormatError, match="no column"):
        load_billing(p)


@pytest.mark.parametrize(
    "body, needle",
    [
        ("at,model,usd\nnot-a-time,m,1\n", ".csv:2"),
        ("at,model,usd\n2026-10-16T10:00:00Z,m,lots\n", "not an amount"),
        ("at,model,usd\n2026-10-16T10:00:00Z,,1\n", "needs at and model"),
        ("at,model,usd\n2026-10-16T10:00:00Z,m,nan\n", "not an amount"),
        ("when,model,usd\n", "no column"),
    ],
)
def test_load_billing_errors_name_the_line(tmp_path: Path, body: str, needle: str):
    p = tmp_path / "openai.csv"
    p.write_text(body, encoding="utf-8", newline="\n")
    with pytest.raises(BillingFormatError, match=needle):
        load_billing(p)
