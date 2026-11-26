import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError
from oex.config.loader import load_config
from omegaconf import OmegaConf

import due as due_module
import events
import jobs as jobs_module
import runner
import schedule as schedule_module
import sweep
from jobs import Job, ScheduleError
from schedule import Schedule

TODAY = date(2026, 8, 5)


@pytest.fixture(autouse=True)
def keep_the_working_directory(monkeypatch):
    monkeypatch.chdir(Path.cwd())


OSM_CONFIG = "iso3: NPL\nsource:\n  osm:\n    enabled: true\n  overture:\n    enabled: false\n"
OVERTURE_CONFIG = "iso3: NPL\nsource:\n  osm:\n    enabled: false\n  overture:\n    enabled: true\n"
BOTH_CONFIG = "iso3: NPL\nsource:\n  osm:\n    enabled: true\n  overture:\n    enabled: true\n"


def write_config(folder, name, body, frequency=None):
    text = f"frequency: {frequency}\n{body}" if frequency else body
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def resolve_all(
    schedule, group=None, frequency=None, today=TODAY, settings=None, workers=None, every=False
):
    return Schedule(schedule, today, frequency, settings, workers, every).resolve(group)


def resolve(schedule, group=None, frequency=None):
    jobs, skipped, broken = resolve_all(schedule, group, frequency)
    assert broken == []
    return jobs, skipped


def broken_files(schedule):
    return resolve_all(schedule, None, None, TODAY)[2]


def countries_schedule(countries, enabled=True):
    return {
        "groups": ["priority"],
        "priority": {"enabled": enabled, "countries": countries},
    }


@pytest.mark.parametrize(
    ("value", "group_default", "attributes"),
    [
        ("daily", None, {"frequency": "daily", "enabled": True}),
        (
            {"frequency": "weekly", "enabled": False},
            "monthly",
            {"frequency": "weekly", "enabled": False},
        ),
        (None, "monthly", {"frequency": "monthly", "enabled": True}),
    ],
)
def test_a_value_is_a_frequency_a_mapping_or_the_group_default(value, group_default, attributes):
    assert schedule_module.normalise_attributes(value, group_default) == attributes


def test_a_value_that_is_neither_is_rejected():
    with pytest.raises(ScheduleError):
        schedule_module.normalise_attributes(7, None)


def test_frequency_filter_selects_matching_jobs_only():
    jobs, _ = resolve(countries_schedule({"NPL": "daily", "SDN": "monthly"}), frequency="daily")
    assert [job.iso3 for job in jobs] == ["NPL"]


def test_frequency_mismatch_is_silent():
    _, skipped = resolve(countries_schedule({"SDN": "monthly"}), frequency="daily")
    assert skipped == []


@pytest.mark.parametrize(
    ("schedule", "reason"),
    [
        (
            countries_schedule({"NPL": {"frequency": "daily", "enabled": False}}),
            "priority/NPL: disabled",
        ),
        (countries_schedule({"NPL": "daily"}, enabled=False), "priority: group disabled"),
        (
            countries_schedule({"NPL": {"frequency": "daily", "expires": date(2026, 8, 4)}}),
            "priority/NPL: expired 2026-08-04",
        ),
    ],
)
def test_a_disabled_or_expired_job_is_skipped_with_a_reason(schedule, reason):
    assert resolve(schedule) == ([], [reason])


def test_expiry_is_inclusive_of_the_final_day():
    entry = {"frequency": "daily", "expires": TODAY}
    jobs, _ = resolve(countries_schedule({"NPL": entry}))
    assert len(jobs) == 1


def test_manual_jobs_stay_out_of_an_unfiltered_sweep():
    jobs, skipped = resolve(countries_schedule({"NPL": jobs_module.MANUAL_FREQUENCY}))
    assert jobs == []
    assert "manual" in skipped[0]


def test_manual_jobs_run_when_asked_for_by_name():
    schedule = countries_schedule({"NPL": jobs_module.MANUAL_FREQUENCY})
    jobs, _ = resolve(schedule, frequency=jobs_module.MANUAL_FREQUENCY)
    assert [job.iso3 for job in jobs] == ["NPL"]


@pytest.mark.parametrize(
    ("schedule", "group", "message"),
    [
        (countries_schedule({"NPL": {"enabled": True}}), None, "no frequency"),
        (countries_schedule({"NPL": "daily"}), "nope", "unknown group"),
        (
            {"groups": ["priority", "ghost"], "priority": {"countries": {"NPL": "daily"}}},
            None,
            "has no block",
        ),
        ({"groups": ["priority"], "priority": {"enabled": True}}, None, "neither"),
    ],
)
def test_a_malformed_schedule_is_an_error(schedule, group, message):
    with pytest.raises(ScheduleError, match=message):
        resolve(schedule, group=group)


def test_groups_run_in_declared_order():
    schedule = {
        "groups": ["second", "first"],
        "first": {"countries": {"AAA": "daily"}},
        "second": {"countries": {"BBB": "daily"}},
    }
    jobs, _ = resolve(schedule)
    assert [job.iso3 for job in jobs] == ["BBB", "AAA"]


@pytest.fixture
def events_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "REPO_ROOT", tmp_path)
    folder = tmp_path / "configs" / "events"
    folder.mkdir(parents=True)
    return folder


def test_folder_job_uses_the_frequency_declared_in_its_own_config(events_dir):
    write_config(events_dir, "quake.yaml", OSM_CONFIG, frequency="daily")
    schedule = {"groups": ["events"], "events": {"dir": "configs/events", "frequency": "monthly"}}
    jobs, _ = resolve(schedule, frequency="daily")
    assert [job.id for job in jobs] == ["events/quake"]


def test_folder_job_falls_back_to_the_group_frequency(events_dir):
    write_config(events_dir, "quake.yaml", OSM_CONFIG)
    schedule = {"groups": ["events"], "events": {"dir": "configs/events", "frequency": "monthly"}}
    jobs, _ = resolve(schedule, frequency="monthly")
    assert [job.id for job in jobs] == ["events/quake"]


def test_a_per_file_override_beats_the_config_and_the_group(events_dir):
    write_config(events_dir, "quake.yaml", OSM_CONFIG, frequency="daily")
    schedule = {
        "groups": ["events"],
        "events": {
            "dir": "configs/events",
            "frequency": "monthly",
            "overrides": {"quake.yaml": "weekly"},
        },
    }
    jobs, _ = resolve(schedule, frequency="weekly")
    assert [job.id for job in jobs] == ["events/quake"]


def test_a_per_file_override_can_disable_one_file(events_dir):
    write_config(events_dir, "quake.yaml", OSM_CONFIG, frequency="daily")
    schedule = {
        "groups": ["events"],
        "events": {"dir": "configs/events", "overrides": {"quake.yaml": {"enabled": False}}},
    }
    jobs, skipped = resolve(schedule, frequency="daily")
    assert jobs == []
    assert skipped == ["events/quake: disabled"]


def test_missing_folder_is_an_error(events_dir):
    schedule = {"groups": ["events"], "events": {"dir": "configs/nowhere", "frequency": "daily"}}
    with pytest.raises(ScheduleError):
        resolve(schedule)


def test_overture_config_resolves_to_the_overture_command(events_dir):
    write_config(events_dir, "quake.yaml", OVERTURE_CONFIG, frequency="daily")
    jobs, _ = resolve({"groups": ["events"], "events": {"dir": "configs/events"}})
    assert [(job.command, job.id) for job in jobs] == [("overture", "events/quake")]


def test_a_config_enabling_both_sources_becomes_two_jobs(events_dir):
    write_config(events_dir, "quake.yaml", BOTH_CONFIG, frequency="daily")
    jobs, _ = resolve({"groups": ["events"], "events": {"dir": "configs/events"}})
    assert [(job.command, job.id) for job in jobs] == [
        ("osm", "events/quake:osm"),
        ("overture", "events/quake:overture"),
    ]


def test_a_source_block_that_omits_a_key_keeps_the_oex_default(events_dir):
    write_config(events_dir, "quake.yaml", "iso3: NPL\nsource:\n  pcodes:\n    enabled: true\n")
    jobs, _ = resolve(
        {"groups": ["events"], "events": {"dir": "configs/events", "frequency": "monthly"}}
    )
    assert [job.command for job in jobs] == ["osm", "overture"]


def test_a_config_without_a_source_block_is_reported_broken(events_dir):
    write_config(events_dir, "quake.yaml", "iso3: NPL\n", frequency="daily")
    [line] = broken_files({"groups": ["events"], "events": {"dir": "configs/events"}})
    assert "source" in line


def test_a_config_with_every_source_disabled_is_reported_broken(events_dir):
    body = "iso3: NPL\nsource:\n  osm:\n    enabled: false\n  overture:\n    enabled: false\n"
    write_config(events_dir, "quake.yaml", body, frequency="daily")
    [line] = broken_files({"groups": ["events"], "events": {"dir": "configs/events"}})
    assert "source" in line


@pytest.fixture
def event_base(events_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    base = tmp_path / "configs" / "_event-base.yaml"
    base.write_text(
        "hdx:\n  site: prod\n  owner_org: hot\nsource:\n  overture:\n    enabled: false\n",
        encoding="utf-8",
    )
    return {"dir": "configs/events", "base": "configs/_event-base.yaml"}


def test_an_event_file_merges_over_the_group_base_and_its_own_values_win(events_dir, event_base):
    write_config(events_dir, "quake.yaml", "iso3: NPL\nhdx:\n  site: stage\n", frequency="daily")
    jobs, _ = resolve({"groups": ["events"], "events": event_base})
    merged = yaml.safe_load(jobs[0].config.read_text(encoding="utf-8"))
    assert (merged["hdx"]["site"], merged["hdx"]["owner_org"]) == ("stage", "hot")


def test_an_event_file_takes_its_sources_from_the_base(events_dir, event_base):
    body = "iso3: NPL\nsource:\n  osm:\n    enabled: true\n"
    write_config(events_dir, "quake.yaml", body, frequency="daily")
    jobs, _ = resolve({"groups": ["events"], "events": event_base})
    assert [job.command for job in jobs] == ["osm"]


def test_a_missing_event_base_is_a_schedule_error(events_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    write_config(events_dir, "quake.yaml", OSM_CONFIG, frequency="daily")
    group = {"dir": "configs/events", "base": "configs/_absent.yaml"}
    with pytest.raises(ScheduleError, match="base"):
        resolve({"groups": ["events"], "events": group})


def test_an_event_group_without_a_base_runs_the_file_as_written(events_dir):
    config = write_config(events_dir, "quake.yaml", OSM_CONFIG, frequency="daily")
    jobs, _ = resolve({"groups": ["events"], "events": {"dir": "configs/events"}})
    assert jobs[0].config == config


def merged_event(jobs):
    return yaml.safe_load(jobs[0].config.read_text(encoding="utf-8"))


def test_an_event_takes_its_country_folder_and_dataset_name_from_its_filename(
    events_dir, event_base
):
    body = "frequency: daily\nhdx:\n  combine: true\n"
    write_config(events_dir, "NPL-FL-2026-pokhara.yaml", body)
    merged = merged_event(resolve({"groups": ["events"], "events": event_base})[0])
    assert merged["iso3"] == "NPL"
    assert merged["output"]["s3"]["folder"] == "NPL_FL_2026_POKHARA"
    assert merged["hdx"]["combined"]["name"] == "hotosm_npl_fl_2026_pokhara"


def test_names_written_in_the_event_file_win_over_the_filename(events_dir, event_base):
    body = "output:\n  s3:\n    folder: NPL_FLOOD\nhdx:\n  combined:\n    name: hot_flood_npl\n"
    write_config(events_dir, "NPL-FL-2026-flood-extent.yaml", body, frequency="daily")
    merged = merged_event(resolve({"groups": ["events"], "events": event_base})[0])
    assert merged["output"]["s3"]["folder"] == "NPL_FLOOD"
    assert merged["hdx"]["combined"]["name"] == "hot_flood_npl"


def test_a_filename_without_an_iso3_prefix_leaves_the_country_to_the_file(events_dir, event_base):
    write_config(events_dir, "DEMO-FL-2026-pokhara.yaml", "frequency: daily\n")
    assert "iso3" not in merged_event(resolve({"groups": ["events"], "events": event_base})[0])


def test_an_event_past_the_expiry_in_its_own_file_is_skipped(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-08-04\n")
    jobs, skipped = resolve({"groups": ["events"], "events": event_base})
    assert jobs == []
    assert skipped == ["events/NPL-FL-2026-a: expired 2026-08-04, nothing to close on HDX"]


def test_the_schedule_expiry_wins_over_the_event_file(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-08-04\n")
    group = {**event_base, "overrides": {"NPL-FL-2026-a.yaml": {"expires": "2026-09-01"}}}
    jobs, skipped = resolve({"groups": ["events"], "events": group})
    assert skipped == []
    assert "expires" not in merged_event(jobs)


def test_an_event_still_holding_the_template_placeholder_is_reported_broken(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "hdx:\n  title: CHANGE_ME\n", "daily")
    [line] = broken_files({"groups": ["events"], "events": event_base})
    assert "CHANGE_ME" in line


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("NPL-FL-2026-pokhara v2.yaml", "letters, digits and -"),
        ("npl-FL-2026-a.yaml", "capitals"),
        (f"NPL-FL-2026-{'x' * 90}.yaml", "over 100 characters"),
    ],
)
def test_an_event_filename_hdx_cannot_take_is_reported_broken(
    events_dir, event_base, filename, message
):
    write_config(events_dir, filename, "frequency: daily\nhdx:\n  combine: true\n")
    [line] = broken_files({"groups": ["events"], "events": event_base})
    assert message in line


def test_an_event_that_does_not_combine_gets_no_dataset_name(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\n")
    merged = merged_event(resolve({"groups": ["events"], "events": event_base})[0])
    assert "combined" not in merged["hdx"]


def test_an_expiry_that_is_not_a_date_is_reported_broken(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: soon\n")
    [line] = broken_files({"groups": ["events"], "events": event_base})
    assert "YYYY-MM-DD" in line


def test_no_hdx_push_reaches_every_job_with_its_country(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    jobs, _, _ = resolve_all(
        countries_schedule({"NPL": "monthly"}), None, None, TODAY, {"hdx.push": False}
    )
    assert jobs[0].overrides() == {"iso3": "NPL", "hdx.push": False}


def test_a_country_without_an_override_still_gets_a_merged_config(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    jobs, _ = resolve(countries_schedule({"NPL": "weekly"}))
    assert jobs[0].iso3 == "NPL"
    assert jobs[0].config != jobs_module.BASE_CONFIG
    assert yaml.safe_load(jobs[0].config.read_text(encoding="utf-8"))["frequency"] == "weekly"


def test_a_country_override_is_merged_over_base_not_substituted(tmp_path, monkeypatch):
    overrides = tmp_path / "countries"
    overrides.mkdir()
    (overrides / "SDN.yaml").write_text("dataset_name: Sudan\n", encoding="utf-8")
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", overrides)
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")

    merged = yaml.safe_load(
        schedule_module.country_config("SDN", "weekly").read_text(encoding="utf-8")
    )
    base = yaml.safe_load(jobs_module.BASE_CONFIG.read_text(encoding="utf-8"))
    assert merged["dataset_name"] == "Sudan"
    assert merged["hdx"]["push"] == base["hdx"]["push"]
    assert merged["categories_file"] == base["categories_file"]
    assert merged["frequency"] == "weekly"


def test_a_merged_country_config_loads_without_publishing_credentials(tmp_path, monkeypatch):
    for name in ("OEX_S3_BUCKET", "HDX_API_KEY", "HDX_OWNER_ORG", "HDX_MAINTAINER"):
        monkeypatch.delenv(name, raising=False)
    overrides = tmp_path / "countries"
    overrides.mkdir()
    (overrides / "SDN.yaml").write_text("dataset_name: Sudan\n", encoding="utf-8")
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", overrides)
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")

    base = load_config(jobs_module.BASE_CONFIG, {"iso3": "SDN"})
    merged = load_config(schedule_module.country_config("SDN", "monthly"), {"iso3": "SDN"})
    assert merged.hdx.push == base.hdx.push is True
    assert merged.output.s3.enabled == base.output.s3.enabled is True
    assert len(merged.categories) == len(base.categories)
    assert merged.dataset_name == "Sudan"


def stub(job_id, config="unused"):
    return Job(job_id, "tests", "osm", Path(config), None)


def succeed(job):
    return True


def fail(job):
    return False


def crash(job):
    raise RuntimeError(f"{job.id} broke")


def hang(job):
    time.sleep(300)


def log_a_line(job):
    logging.getLogger("oex.exporter").error("boom")
    return False


def touch_then_hang(job):
    job.config.touch()
    time.sleep(300)


def leave_a_grandchild(job):
    inner = subprocess.Popen(["sleep", "300"])
    job.config.write_text(str(inner.pid))
    inner.wait()


def leave_a_grandchild_ignoring_sigterm(job):
    inner = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 300 & wait"])
    job.config.write_text(str(inner.pid))
    inner.wait()


def touch_then_ignore_sigterm(job):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    job.config.touch()
    time.sleep(300)


def mark_on_sigterm(job):
    signal.signal(signal.SIGTERM, lambda *_: (job.config.touch(), sys.exit(0)))
    time.sleep(300)


def test_a_failing_job_is_reported_and_the_sweep_continues(capsys):
    assert runner.Runner(30, fail).run([stub("a"), stub("b")]) == ["a", "b"]
    assert runner.Runner(30, succeed).run([stub("c")]) == []
    assert "c done" in capsys.readouterr().out


def test_a_job_that_raises_logs_the_reason_under_its_id(capfd):
    assert runner.Runner(30, crash).run([stub("bad"), stub("other")], 2) == ["bad", "other"]
    lines = capfd.readouterr().err.splitlines()
    assert any(line.startswith("bad ") and "bad FAILED" in line for line in lines)
    assert "RuntimeError: bad broke" in lines


class Result:
    succeeded, unchanged, empty, skipped, total_duration_s = 3, 0, 0, 0, 1.0

    def __init__(self, failed):
        self.failed = failed


@pytest.mark.parametrize(("failed", "succeeded"), [(0, True), (1, False)])
def test_an_export_succeeds_only_when_no_layer_failed(tmp_path, monkeypatch, failed, succeeded):
    config = write_config(tmp_path, "npl.yaml", OSM_CONFIG)
    monkeypatch.setattr(
        runner, "Exporter", lambda cfg, source: type("E", (), {"run": lambda s: Result(failed)})()
    )
    assert runner.export(Job("tests/npl", "tests", "osm", config, "NPL")) is succeeded


def test_every_log_line_a_job_writes_starts_with_its_id(capfd):
    runner.Runner(30, log_a_line).run([stub("normal/ZWE")])
    err = capfd.readouterr().err
    assert any(
        line.startswith("normal/ZWE ") and line.endswith("[ERROR] oex.exporter: boom")
        for line in err.splitlines()
    )
    assert "normal/ZWE FAILED rc=1" in err


def test_a_job_that_overruns_its_timeout_is_a_failure(capsys):
    assert runner.Runner(1, hang).run([stub("slow")]) == ["slow"]
    assert "TIMEOUT" in capsys.readouterr().err


def _gone_within(pid_file: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not pid_file.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_timeout_stops_the_process_the_job_started_too(tmp_path):
    pid_file = tmp_path / "inner.pid"
    pid_file.touch()
    assert runner.Runner(5, leave_a_grandchild).run([stub("slow", pid_file)]) == ["slow"]
    assert _gone_within(pid_file, seconds=2)


def test_an_inner_process_ignoring_sigterm_is_killed_after_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STOP_GRACE_SECONDS", 1)
    pid_file = tmp_path / "inner.pid"
    pid_file.touch()
    job = stub("stubborn", pid_file)
    assert runner.Runner(5, leave_a_grandchild_ignoring_sigterm).run([job]) == ["stubborn"]
    assert _gone_within(pid_file, seconds=3)


def test_a_timed_out_job_gets_sigterm_and_time_to_shut_down(tmp_path):
    marker = tmp_path / "stopped"
    assert runner.Runner(5, mark_on_sigterm).run([stub("slow", marker)]) == ["slow"]
    assert marker.exists()


def test_ctrl_c_stops_running_jobs_and_starts_no_queued_one(tmp_path, capfd):
    started = [tmp_path / "a", tmp_path / "b"]
    late = tmp_path / "late"
    jobs = [stub(f.name, f) for f in [*started, late]]

    def interrupt_once_both_run():
        deadline = time.monotonic() + 30
        while not all(f.exists() for f in started) and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    job_runner = runner.Runner(60, touch_then_hang)
    threading.Thread(target=interrupt_once_both_run, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        job_runner.run(jobs, 2)
    time.sleep(2)
    assert not late.exists()
    assert job_runner._running == set()
    err = capfd.readouterr().err
    assert "a STOPPED" in err and "b STOPPED" in err
    assert "TIMEOUT" not in err


def test_a_second_sweep_cannot_take_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    held = runner.acquire_lock()
    assert held is not None
    assert runner.acquire_lock() is None
    held.close()
    assert runner.acquire_lock() is not None


def test_a_waiting_sweep_takes_the_lock_once_it_is_released(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    held = runner.acquire_lock()
    assert held is not None
    threading.Timer(0.5, held.close).start()
    started = time.monotonic()
    assert runner.acquire_lock(wait=True) is not None
    assert time.monotonic() - started >= 0.4


def test_the_shipped_schedule_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    schedule = yaml.safe_load(jobs_module.SCHEDULE_FILE.read_text(encoding="utf-8"))
    jobs, skipped = resolve(schedule, frequency="monthly")
    assert jobs
    assert skipped == []
    assert all(job.command in jobs_module.COMMAND_SOURCES for job in jobs)
    country_jobs = [job for job in jobs if job.group != "events"]
    assert all(len(job.iso3) == 3 and job.iso3.isupper() for job in country_jobs)
    assert len({job.id for job in jobs}) == len(jobs)


def test_a_dir_group_skips_underscore_partials(tmp_path, monkeypatch):
    folder = tmp_path / "events"
    folder.mkdir()
    (folder / "real.yaml").write_text(OSM_CONFIG, encoding="utf-8")
    (folder / "_shared-categories.yaml").write_text("categories: []\n", encoding="utf-8")
    monkeypatch.setattr(jobs_module, "REPO_ROOT", tmp_path)
    schedule = {"groups": ["events"], "events": {"dir": "events", "frequency": "daily"}}
    jobs, _ = resolve(schedule, frequency="daily")
    assert [job.config.name for job in jobs] == ["real.yaml"]


def test_no_resume_reaches_the_oex_command(monkeypatch, tmp_path):
    [batch] = main_with(
        monkeypatch, tmp_path, ["--no-resume"], countries_schedule({"NPL": "monthly"})
    )
    assert batch["jobs"][0].overrides()["output.resume"] is False


def test_only_narrows_the_job_list(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    jobs, _ = resolve(countries_schedule({"NPL": "monthly", "SDN": "monthly", "TCD": "monthly"}))
    assert [j.iso3 for j in schedule_module.select_countries(jobs, "NPL,TCD")] == ["NPL", "TCD"]


def test_only_rejects_a_code_with_no_job(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    jobs, _ = resolve(countries_schedule({"NPL": "monthly"}))
    with pytest.raises(ScheduleError, match="VNM"):
        schedule_module.select_countries(jobs, "NPL,VNM")


def test_workers_bound_peak_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    peak = 0
    running = 0
    lock = threading.Lock()

    def observe(self, job):
        nonlocal peak, running
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        return None

    monkeypatch.setattr(runner.Runner, "run_one", observe)
    runner.Runner(30).run([stub(f"j{i}") for i in range(8)], 3)
    assert peak == 3, f"peak concurrency was {peak}, expected 3"


@pytest.mark.parametrize(("memory_gb", "workers", "share"), [(38, 1, 38), (38, 4, 9), (1, 8, 1)])
def test_the_memory_budget_is_divided_between_workers_and_never_below_one_gb(
    monkeypatch, memory_gb, workers, share
):
    monkeypatch.setattr(schedule_module, "adaptive_parallel_resources", lambda: (1, memory_gb))
    assert schedule_module.memory_gb_per_worker(workers) == share


@pytest.mark.parametrize("budget", [7, None])
def test_only_a_budget_reaches_the_merged_config(tmp_path, monkeypatch, budget):
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    merged = OmegaConf.load(schedule_module.country_config("SDN", "monthly", budget))
    assert OmegaConf.select(merged, "parallel.memory_gb") == budget


def use_schedule(monkeypatch, tmp_path, schedule, *argv):
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(yaml.safe_dump(schedule), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(sys, "argv", ["sweep", *argv])


def main_with(monkeypatch, tmp_path, argv, schedule, exit_code=0):
    use_schedule(monkeypatch, tmp_path, schedule, *argv)
    monkeypatch.setattr(runner, "acquire_lock", lambda wait: object())
    batches = []

    def record(self, batch, workers=1):
        batches.append({"jobs": batch, "timeout": self.timeout, "workers": workers})
        return []

    monkeypatch.setattr(runner.Runner, "run", record)
    assert sweep.main() == exit_code
    return batches


def test_only_narrows_the_run_to_the_named_countries(monkeypatch, tmp_path):
    schedule = countries_schedule({"NPL": "monthly", "HTI": "monthly", "CHL": "monthly"})
    [batch] = main_with(monkeypatch, tmp_path, ["--only", "NPL"], schedule)
    assert [job.iso3 for job in batch["jobs"]] == ["NPL"]


def test_workers_reach_the_runner_and_give_each_country_a_memory_budget(monkeypatch, tmp_path):
    schedule = countries_schedule({"NPL": "monthly"})
    [batch] = main_with(monkeypatch, tmp_path, ["--workers", "4"], schedule)
    assert batch["workers"] == 4
    merged = yaml.safe_load(Path(batch["jobs"][0].config).read_text(encoding="utf-8"))
    assert merged["parallel"]["memory_gb"] == schedule_module.memory_gb_per_worker(4)


def test_a_country_takes_the_group_frequency_unless_it_names_its_own(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    schedule = {
        "groups": ["normal"],
        "normal": {"frequency": "monthly", "countries": {"PAK": "weekly", "NPL": None}},
    }
    jobs, _ = resolve(schedule)
    assert {job.iso3: job.frequency for job in jobs} == {"PAK": "weekly", "NPL": "monthly"}


def test_a_folder_group_cannot_run_parallel_workers(tmp_path, monkeypatch):
    folder = tmp_path / "events"
    folder.mkdir()
    write_config(folder, "flood.yaml", OSM_CONFIG, frequency="daily")
    monkeypatch.setattr(jobs_module, "REPO_ROOT", tmp_path)
    schedule = {"groups": ["events"], "events": {"dir": "events", "workers": 2}}
    with pytest.raises(ScheduleError, match="one job at a time"):
        resolve(schedule)


def _job(frequency: str, group: str = "normal") -> Job:
    return Job(f"{group}/NPL", group, "osm", Path("npl.yaml"), "NPL", frequency=frequency)


NOW = datetime(2026, 12, 1, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("frequency", "days_since", "due"),
    [
        ("monthly", None, True),
        ("monthly", 29, False),
        ("monthly", 30, True),
        ("quarterly", 89, False),
        ("quarterly", 90, True),
        ("yearly", 364, False),
        ("daily", 1, True),
        ("as needed", 1000, False),
    ],
)
def test_a_job_is_due_once_its_frequency_has_passed(frequency, days_since, due):
    last = None if days_since is None else NOW - timedelta(days=days_since)
    assert due_module.is_due(_job(frequency), last, NOW) is due


@pytest.mark.parametrize("frequency", ["sometimes", "30"])
def test_a_frequency_hdx_does_not_name_is_a_schedule_error(frequency):
    with pytest.raises(ScheduleError, match="not an HDX frequency"):
        due_module.is_due(_job(frequency), None, NOW)


def test_due_runs_each_group_in_order_with_its_own_workers(monkeypatch, tmp_path):
    schedule = {
        "groups": ["priority", "normal"],
        "priority": {"frequency": "monthly", "countries": ["SDN"]},
        "normal": {"frequency": "monthly", "workers": 2, "countries": ["NPL", "BTN"]},
    }
    monkeypatch.setattr(
        due_module, "last_publish", lambda job: datetime.now(UTC) if job.iso3 == "BTN" else None
    )
    batches = main_with(monkeypatch, tmp_path, ["--due"], schedule)
    assert [([job.iso3 for job in b["jobs"]], b["workers"]) for b in batches] == [
        (["SDN"], 1),
        (["NPL"], 2),
    ]


def test_a_daily_job_that_finished_after_yesterdays_tick_is_due_today():
    assert due_module.is_due(_job("daily"), NOW - timedelta(hours=23, minutes=40), NOW)


def test_the_workers_option_overrides_country_groups_only(monkeypatch, tmp_path):
    folder = tmp_path / "events"
    folder.mkdir()
    write_config(folder, "flood.yaml", OSM_CONFIG, frequency="daily")
    monkeypatch.setattr(jobs_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    schedule = {
        "groups": ["normal", "events"],
        "normal": {"frequency": "monthly", "workers": 2, "countries": ["NPL"]},
        "events": {"dir": "events"},
    }
    jobs, _, _ = resolve_all(schedule, None, None, TODAY, workers=3)
    assert {job.group: job.workers for job in jobs} == {"normal": 3, "events": 1}


ITEM_CONFIG = (
    "key: hotosm\n"
    + OSM_CONFIG
    + "hdx:\n  push: true\n"
    + "output:\n  metadata: true\n  s3:\n    enabled: true\n    bucket: exports\n    prefix: ISO3\n"
    + "categories:\n  - name: Roads\n  - name: Schools\n  - name: Buildings\n"
    + "  - name: Places\n    osm:\n      enabled: false\n"
)


class ItemStore:
    def __init__(self, items: dict[str, dict]):
        self.items = items

    def read(self, key):
        return self.items.get(key), None


def _published(tmp_path, monkeypatch, items: dict[str, dict]) -> Job:
    config = write_config(tmp_path, "npl.yaml", ITEM_CONFIG)
    monkeypatch.setattr(due_module, "S3CatalogStore", lambda cfg: ItemStore(items))
    return Job("normal/NPL", "normal", "osm", config, "NPL", frequency="monthly")


def _key(category: str, source: str = "osm") -> str:
    return f"ISO3/NPL/{category}/hotosm_npl_{category}_{source}_item.json"


def _item(updated: str | None) -> dict:
    return {"properties": {"updated": updated} if updated else {}}


def test_the_last_publish_is_the_oldest_configured_layer_of_the_jobs_source(tmp_path, monkeypatch):
    job = _published(
        tmp_path,
        monkeypatch,
        {
            _key("roads"): _item("2026-12-01T02:30:00Z"),
            _key("schools"): _item("2026-11-20T03:00:00Z"),
            _key("buildings"): _item("2026-11-25T03:00:00Z"),
            _key("buildings", "overture"): _item("2026-01-01T00:00:00Z"),
            _key("retired_layer"): _item("2025-01-01T00:00:00Z"),
        },
    )
    assert due_module.last_publish(job) == datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def test_a_newly_configured_category_makes_the_job_due(tmp_path, monkeypatch):
    published = {_key(name): _item("2026-11-20T03:00:00Z") for name in ("roads", "schools")}
    assert due_module.last_publish(_published(tmp_path, monkeypatch, published)) is None


def test_a_layer_whose_publish_failed_makes_the_job_due(tmp_path, monkeypatch):
    published = {_key(name): _item("2026-12-01T00:00:00Z") for name in ("roads", "schools")}
    published[_key("buildings")] = _item(None)
    assert due_module.last_publish(_published(tmp_path, monkeypatch, published)) is None


@pytest.mark.parametrize(
    ("setting", "turned_off"),
    [
        ("  s3:\n    enabled: true\n", "  s3:\n    enabled: false\n"),
        ("  metadata: true\n", "  metadata: false\n  report:\n    enabled: false\n"),
        ("  push: true\n", "  push: false\n"),
    ],
    ids=["s3-off", "metadata-off", "hdx-push-off"],
)
def test_a_job_oex_writes_no_items_for_is_always_due(tmp_path, setting, turned_off):
    config = write_config(tmp_path, "tm.yaml", ITEM_CONFIG.replace(setting, turned_off, 1))
    job = Job("tasking_manager/1", "tasking_manager", "osm", config, None, frequency="daily")
    assert due_module.last_publish(job) is None


def test_a_category_the_source_cannot_export_is_left_out(tmp_path, monkeypatch):
    categories = ITEM_CONFIG[ITEM_CONFIG.index("categories:") :]
    body = ITEM_CONFIG.replace(categories, "") + (
        "categories:\n"
        "  - name: Roads\n    overture:\n      theme: transportation\n      feature_type: segment\n"
        "  - name: Lakes\n    overture:\n      enabled: true\n"
    )
    config = write_config(tmp_path, "npl.yaml", body)
    items = {_key("roads", "overture"): _item("2026-12-01T00:00:00Z")}
    monkeypatch.setattr(due_module, "S3CatalogStore", lambda cfg: ItemStore(items))
    job = Job("normal/NPL", "normal", "overture", config, "NPL", frequency="monthly")
    assert due_module.last_publish(job) == datetime(2026, 12, 1, tzinfo=UTC)


def _unloadable_config(tmp_path, monkeypatch):
    monkeypatch.delenv("OEX_UNSET_PBF", raising=False)
    unset_pbf = OSM_CONFIG.replace(
        "enabled: true\n", "enabled: true\n    pbf_path: ${oc.env:OEX_UNSET_PBF}\n", 1
    )
    return write_config(tmp_path, "1.yaml", unset_pbf)


def test_a_folder_config_oex_cannot_load_is_left_out_and_named(tmp_path, monkeypatch):
    config = _unloadable_config(tmp_path, monkeypatch)
    job = Job("tasking_manager/1", "tasking_manager", "osm", config, None, frequency="daily")
    due, [line] = due_module.due_jobs([job], NOW)
    assert due == []
    assert line.startswith("tasking_manager/1: ")


def test_a_country_config_oex_cannot_load_stops_the_sweep(tmp_path, monkeypatch):
    config = _unloadable_config(tmp_path, monkeypatch)
    job = Job("normal/NPL", "normal", "osm", config, "NPL", frequency="daily")
    with pytest.raises(ScheduleError, match="normal/NPL: cannot read"):
        due_module.due_jobs([job], NOW)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("frequency: daily\nhdx: [\n", "not valid YAML"),
        ("frequency: daily\nexpires: 2026-11-31\n", "not valid YAML"),
        ("- frequency: daily\n", "not a YAML mapping"),
        ("just some text\n", "not a YAML mapping"),
        ("frequency: daily\nhdx:\n  site: stage\nhdx:\n  site: prod\n", "hdx"),
        ("frequency: every 3 days\n", "not an HDX frequency name"),
    ],
)
def test_an_event_file_an_author_got_wrong_is_reported_broken(
    events_dir, event_base, body, message
):
    (events_dir / "NPL-FL-2026-a.yaml").write_text(body, encoding="utf-8")
    write_config(events_dir, "NPL-FL-2026-b.yaml", "frequency: daily\n")
    jobs, _, [line] = resolve_all({"groups": ["events"], "events": event_base}, None, None, TODAY)
    assert [job.id for job in jobs] == ["events/NPL-FL-2026-b"]
    assert line.startswith("events/NPL-FL-2026-a: ")
    assert message in line


def test_an_event_without_any_frequency_is_reported_broken(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "hdx:\n  site: stage\n")
    [line] = broken_files({"groups": ["events"], "events": event_base})
    assert "no frequency" in line


def test_a_broken_event_leaves_the_other_events_and_countries_to_run(
    events_dir, event_base, tmp_path, monkeypatch, capsys
):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nhdx:\n  title: CHANGE_ME\n")
    write_config(events_dir, "NPL-FL-2026-b.yaml", "frequency: daily\n")
    schedule = {
        "groups": ["priority", "events"],
        "priority": {"countries": {"NPL": "daily"}},
        "events": event_base,
    }
    batches = main_with(monkeypatch, tmp_path, [], schedule, exit_code=1)
    assert [job.id for b in batches for job in b["jobs"]] == [
        "priority/NPL",
        "events/NPL-FL-2026-b",
    ]
    assert "broken events/NPL-FL-2026-a" in capsys.readouterr().err


def test_a_config_due_cannot_load_makes_the_sweep_fail_after_the_rest(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(due_module, "due_jobs", lambda jobs, now: (jobs, ["events/x: bad key"]))
    schedule = countries_schedule({"NPL": "daily"})
    [batch] = main_with(monkeypatch, tmp_path, ["--due"], schedule, exit_code=1)
    assert [job.id for job in batch["jobs"]] == ["priority/NPL"]
    assert "broken events/x: bad key" in capsys.readouterr().err


class MemoryStore:
    def __init__(self):
        self.documents = {}

    def read(self, key):
        document = self.documents.get(key)
        return document, None if document is None else "etag"

    def write(self, key, document, version):
        self.documents[key] = document


@pytest.fixture
def closed_store(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(events, "S3CatalogStore", lambda cfg: store)
    monkeypatch.setattr(events, "writes_items", lambda cfg: False)
    return store


@pytest.fixture
def pushing_base(event_base, tmp_path, closed_store):
    (tmp_path / "configs" / "_event-base.yaml").write_text(
        "hdx:\n  push: true\n  site: stage\n  combine: true\n"
        "output:\n  stac:\n    catalog: true\n  s3:\n    enabled: true\n    bucket: exports\n"
        "source:\n  overture:\n    enabled: false\n",
        encoding="utf-8",
    )
    return event_base


def record_close(store, job):
    store.documents[events.closed_key(load_config(job.config))] = {"expires": job.closes}


def expired_event(events_dir, frequency="daily"):
    body = f"frequency: {frequency}\nexpires: 2026-08-04\nsource:\n  osm:\n    enabled: true\n"
    return write_config(events_dir, "NPL-FL-2026-a.yaml", body)


def test_an_expired_event_gets_one_job_that_sets_hdx_to_as_needed(events_dir, pushing_base):
    expired_event(events_dir)
    jobs, skipped = resolve({"groups": ["events"], "events": pushing_base})
    [job] = jobs
    assert (job.id, job.command) == ("events/NPL-FL-2026-a:close", "metadata")
    assert yaml.safe_load(job.config.read_text(encoding="utf-8"))["frequency"] == "as needed"
    assert skipped == ["events/NPL-FL-2026-a: expired 2026-08-04"]


def test_an_event_closed_once_is_not_closed_again(events_dir, pushing_base, closed_store):
    expired_event(events_dir)
    schedule = {"groups": ["events"], "events": pushing_base}
    [first] = resolve(schedule)[0]
    record_close(closed_store, first)
    jobs, skipped = resolve(schedule)
    assert jobs == []
    assert skipped == ["events/NPL-FL-2026-a: expired 2026-08-04, closed on HDX"]


def test_the_close_is_recorded_beside_the_events_stac_catalog(events_dir, pushing_base):
    expired_event(events_dir)
    [job] = resolve({"groups": ["events"], "events": pushing_base})[0]
    assert events.closed_key(load_config(job.config)) == "NPL_FL_2026_A/closed.json"


def test_an_event_without_a_stac_catalog_is_left_for_a_person_to_close(events_dir, pushing_base):
    body = "frequency: daily\nexpires: 2026-08-04\noutput:\n  stac:\n    catalog: false\n"
    write_config(events_dir, "NPL-FL-2026-a.yaml", body)
    jobs, skipped = resolve({"groups": ["events"], "events": pushing_base})
    assert jobs == []
    assert "no STAC catalog on S3" in skipped[0]


def test_an_event_closed_before_its_expiry_moved_is_closed_again(
    events_dir, pushing_base, closed_store
):
    schedule = {"groups": ["events"], "events": pushing_base}
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-08-01\n")
    [first] = resolve(schedule)[0]
    record_close(closed_store, first)
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-08-04\n")
    jobs, _ = resolve(schedule)
    assert [job.id for job in jobs] == ["events/NPL-FL-2026-a:close"]


@pytest.mark.parametrize(
    ("frequency", "settings"), [("daily", {"hdx.push": False}), ("as needed", {})]
)
def test_an_expired_event_is_not_closed_without_pushing_or_when_already_as_needed(
    events_dir, pushing_base, frequency, settings
):
    expired_event(events_dir, frequency)
    schedule = {"groups": ["events"], "events": pushing_base}
    jobs, _, _ = resolve_all(schedule, None, None, TODAY, settings)
    assert jobs == []


def test_a_closing_job_is_due_without_reading_the_catalog(tmp_path, monkeypatch):
    def unreachable(job):
        raise AssertionError(job.id)

    monkeypatch.setattr(due_module, "last_publish", unreachable)
    job = Job("events/a:close", "events", "metadata", tmp_path, None, closes="2026-08-04")
    assert due_module.due_jobs([job], NOW) == ([job], [])


class Publisher:
    def __init__(self, hdx, error=None):
        self.error = error

    def update_metadata(self, cfg):
        if self.error:
            raise self.error
        return "hotosm_npl_fl_2026_a", 1, 0


@pytest.mark.parametrize(("error", "closed"), [(None, True), (RuntimeError("HDX is down"), False)])
def test_only_a_closing_job_that_updates_hdx_records_the_close(
    events_dir, pushing_base, closed_store, monkeypatch, error, closed
):
    expired_event(events_dir)
    [job] = resolve({"groups": ["events"], "events": pushing_base})[0]
    monkeypatch.setattr(runner, "HdxPublisher", lambda hdx: Publisher(hdx, error))
    monkeypatch.setattr(runner, "S3CatalogStore", lambda cfg: closed_store)
    if error:
        with pytest.raises(RuntimeError):
            runner.export(job)
    else:
        assert runner.export(job)
    assert (closed_store.documents != {}) is closed
    assert resolve({"groups": ["events"], "events": pushing_base})[0] == ([] if closed else [job])


@pytest.mark.parametrize(
    ("hdx", "note"),
    [
        ("  push: false\n", "nothing to close on HDX"),
        ("  combine: false\n", "not combined, so set its HDX frequency by hand"),
    ],
)
def test_an_expired_event_hdx_cannot_be_closed_for_is_skipped_with_the_reason(
    events_dir, pushing_base, hdx, note
):
    body = f"frequency: daily\nexpires: 2026-08-04\nhdx:\n{hdx}"
    write_config(events_dir, "NPL-FL-2026-a.yaml", body)
    jobs, skipped = resolve({"groups": ["events"], "events": pushing_base})
    assert jobs == []
    assert skipped == [f"events/NPL-FL-2026-a: expired 2026-08-04, {note}"]


def run_check(tmp_path, monkeypatch, schedule):
    use_schedule(monkeypatch, tmp_path, schedule, "--check")
    monkeypatch.setattr(runner.Runner, "run", lambda *args: pytest.fail("--check ran a job"))
    return sweep.main()


def test_check_loads_disabled_and_expired_configs_and_names_the_broken_one(
    events_dir, event_base, tmp_path, monkeypatch, capsys
):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-01-01\n")
    write_config(events_dir, "NPL-FL-2026-b.yaml", "frequency: daily\nhdx:\n  titel: typo\n")
    write_config(events_dir, "NPL-FL-2026-c.yaml", "frequency: daily\nexpires: end of March\n")
    schedule = {"groups": ["events"], "events": {**event_base, "enabled": False}}
    assert run_check(tmp_path, monkeypatch, schedule) == 1
    out, err = capsys.readouterr()
    assert "checked 2 job(s), 2 broken" in out
    assert "broken events/NPL-FL-2026-b: " in err
    assert "titel" in err
    assert "broken events/NPL-FL-2026-c: expires 'end of March'" in err


def test_check_passes_when_every_config_loads(
    events_dir, event_base, tmp_path, monkeypatch, capsys
):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\n")
    assert run_check(tmp_path, monkeypatch, {"groups": ["events"], "events": event_base}) == 0
    assert "checked 1 job(s), 0 broken" in capsys.readouterr().out


class AnyKeyStore:
    def __init__(self, updated):
        self.updated = updated

    def read(self, key):
        return _item(self.updated), None


@pytest.mark.parametrize(("updated", "closes"), [(None, False), ("2026-08-01T00:00:00Z", True)])
def test_an_expired_event_is_closed_only_if_a_layer_ever_reached_hdx(
    events_dir, pushing_base, monkeypatch, updated, closes
):
    expired_event(events_dir)
    monkeypatch.setattr(events, "writes_items", lambda cfg: True)
    monkeypatch.setattr(events, "S3CatalogStore", lambda cfg: AnyKeyStore(updated))
    jobs, skipped = resolve({"groups": ["events"], "events": pushing_base})
    assert [job.id for job in jobs] == (["events/NPL-FL-2026-a:close"] if closes else [])
    if not closes:
        assert skipped == [
            "events/NPL-FL-2026-a: expired 2026-08-04, never published, nothing to close on HDX"
        ]


class OneLayerDatedStore:
    def read(self, key):
        return _item("2026-08-01T00:00:00Z" if "buildings" in key else None), None


def test_one_layer_that_reached_hdx_is_enough_to_close_the_event(
    events_dir, pushing_base, monkeypatch
):
    body = (
        "frequency: daily\nexpires: 2026-08-04\ncategories:\n  - name: buildings\n  - name: roads\n"
    )
    write_config(events_dir, "NPL-FL-2026-a.yaml", body)
    monkeypatch.setattr(events, "writes_items", lambda cfg: True)
    monkeypatch.setattr(events, "S3CatalogStore", lambda cfg: OneLayerDatedStore())
    jobs, _ = resolve({"groups": ["events"], "events": pushing_base})
    assert [job.id for job in jobs] == ["events/NPL-FL-2026-a:close"]


def test_an_event_whose_layers_cannot_be_read_is_reported_broken(
    events_dir, pushing_base, monkeypatch
):
    class FailingStore:
        def read(self, key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    expired_event(events_dir)
    monkeypatch.setattr(events, "writes_items", lambda cfg: True)
    monkeypatch.setattr(events, "S3CatalogStore", lambda cfg: FailingStore())
    [line] = broken_files({"groups": ["events"], "events": pushing_base})
    assert "cannot read its published layers" in line


def test_due_jobs_keep_closing_jobs_in_their_group_order(tmp_path, monkeypatch):
    monkeypatch.setattr(due_module, "last_publish", lambda job: None)
    close = Job("events/a:close", "events", "metadata", tmp_path, None, closes="2026-08-04")
    later = Job("tm/1", "tm", "osm", tmp_path, None, frequency="daily")
    assert due_module.due_jobs([close, later], NOW) == ([close, later], [])


def test_an_event_without_its_own_frequency_publishes_the_groups(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "source:\n  osm:\n    enabled: true\n")
    group = {**event_base, "frequency": "weekly"}
    [job], _ = resolve({"groups": ["events"], "events": group})
    assert yaml.safe_load(job.config.read_text(encoding="utf-8"))["frequency"] == "weekly"


def test_a_sweep_takes_the_lock_before_it_decides_what_is_due(monkeypatch, tmp_path):
    order = []
    use_schedule(monkeypatch, tmp_path, countries_schedule({"NPL": "monthly"}), "--due", "--wait")
    monkeypatch.setattr(runner, "acquire_lock", lambda wait: order.append("lock") or object())
    monkeypatch.setattr(due_module, "due_jobs", lambda jobs, now: order.append("due") or ([], []))
    assert sweep.main() == 0
    assert order == ["lock", "due"]


@pytest.mark.parametrize(("argv", "wait"), [([], False), (["--wait"], True)])
def test_wait_reaches_the_lock(monkeypatch, tmp_path, argv, wait):
    asked = []
    use_schedule(monkeypatch, tmp_path, countries_schedule({"NPL": "monthly"}), *argv)
    monkeypatch.setattr(runner, "acquire_lock", lambda wait: asked.append(wait))
    assert sweep.main() == 3
    assert asked == [wait]


def test_a_job_stopped_right_after_it_starts_stops_within_the_grace(tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(runner, "STOP_GRACE_SECONDS", 20)
    job_runner = runner.Runner(60, hang)

    def interrupt_once_started():
        deadline = time.monotonic() + 10
        while not job_runner._running and time.monotonic() < deadline:
            time.sleep(0.001)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_once_started, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        job_runner.run([stub("early")])
    assert time.monotonic() - started < 15
    assert "early STOPPED" in capfd.readouterr().err


def test_jobs_stopped_together_share_one_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STOP_GRACE_SECONDS", 3)
    markers = [tmp_path / name for name in "abc"]
    jobs = [stub(marker.name, marker) for marker in markers]

    def interrupt_once_all_run():
        deadline = time.monotonic() + 30
        while not all(m.exists() for m in markers) and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_once_all_run, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        runner.Runner(60, touch_then_ignore_sigterm).run(jobs, 3)
    stopped = time.time()
    assert all(marker.exists() for marker in markers)
    assert stopped - max(marker.stat().st_mtime for marker in markers) < 3 * 2


@pytest.mark.parametrize("stop", ["ctrl_c", "sigterm", "waiting_for_the_lock"])
def test_a_stopped_sweep_exits_130(monkeypatch, tmp_path, capsys, stop):
    use_schedule(monkeypatch, tmp_path, countries_schedule({"NPL": "monthly"}), "--wait")
    handlers = {}
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handlers.update({signum: handler})
    )

    def lock(wait):
        if stop == "waiting_for_the_lock":
            raise KeyboardInterrupt
        return object()

    def stopped(self, batch, workers=1):
        if stop == "ctrl_c":
            raise KeyboardInterrupt
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(runner, "acquire_lock", lock)
    monkeypatch.setattr(runner.Runner, "run", stopped)
    assert sweep.main() == 130
    assert "sweep: stopped" in capsys.readouterr().err


def test_a_merged_config_is_replaced_whole_and_leaves_no_staging_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    target = jobs_module.write_merged("events/x.yaml", OmegaConf.create({"frequency": "weekly"}))
    with target.open(encoding="utf-8") as reader:
        jobs_module.write_merged("events/x.yaml", OmegaConf.create({"frequency": "monthly"}))
        assert reader.read() == "frequency: weekly\n"
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"frequency": "monthly"}
    assert [path.name for path in target.parent.iterdir()] == ["x.yaml"]


def record_pid_then_ignore_sigterm(job):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    job.config.write_text(str(os.getpid()))
    time.sleep(30)


def test_a_second_ctrl_c_during_the_stop_still_kills_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STOP_GRACE_SECONDS", 2)
    pid_file = tmp_path / "job.pid"
    pid_file.touch()

    def interrupt_twice_once_running():
        deadline = time.monotonic() + 30
        while not pid_file.read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_twice_once_running, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        runner.Runner(60, record_pid_then_ignore_sigterm).run([stub("stubborn", pid_file)])
    assert _gone_within(pid_file, seconds=2)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_check_loads_the_shipped_configs_from_any_working_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(sys, "argv", ["sweep", "--check", "--group", "priority"])
    assert sweep.main() == 0
    assert "0 broken" in capsys.readouterr().out
