import os
import signal
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError
from oex.config.loader import load_config

import due as due_module
import events
import jobs as jobs_module
import runner
import schedule as schedule_module
import sweep
from jobs import Job, ScheduleError
from schedule import Schedule

TODAY = date(2026, 8, 5)

OSM_CONFIG = "iso3: NPL\nsource:\n  osm:\n    enabled: true\n  overture:\n    enabled: false\n"
OVERTURE_CONFIG = "iso3: NPL\nsource:\n  osm:\n    enabled: false\n  overture:\n    enabled: true\n"
BOTH_CONFIG = "iso3: NPL\nsource:\n  osm:\n    enabled: true\n  overture:\n    enabled: true\n"


def write_config(folder, name, body, frequency=None):
    text = f"frequency: {frequency}\n{body}" if frequency else body
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def resolve_all(
    schedule, group=None, frequency=None, today=TODAY, extra=(), workers=None, every=False
):
    return Schedule(schedule, today, frequency, extra, workers, every).resolve(group)


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


def test_bare_value_is_a_frequency():
    assert schedule_module.normalise_attributes("daily", None) == {
        "frequency": "daily",
        "enabled": True,
    }


def test_mapping_value_overrides_the_group_default():
    attrs = schedule_module.normalise_attributes(
        {"frequency": "weekly", "enabled": False}, "monthly"
    )
    assert attrs == {"frequency": "weekly", "enabled": False}


def test_missing_value_falls_back_to_the_group_default():
    assert schedule_module.normalise_attributes(None, "monthly")["frequency"] == "monthly"


def test_a_value_that_is_neither_is_rejected():
    with pytest.raises(ScheduleError):
        schedule_module.normalise_attributes(7, None)


def test_frequency_filter_selects_matching_jobs_only():
    jobs, _ = resolve(countries_schedule({"NPL": "daily", "SDN": "monthly"}), frequency="daily")
    assert [job.iso3 for job in jobs] == ["NPL"]


def test_frequency_mismatch_is_silent():
    _, skipped = resolve(countries_schedule({"SDN": "monthly"}), frequency="daily")
    assert skipped == []


def test_disabled_job_is_skipped_with_a_reason():
    jobs, skipped = resolve(countries_schedule({"NPL": {"frequency": "daily", "enabled": False}}))
    assert jobs == []
    assert skipped == ["priority/NPL: disabled"]


def test_disabled_group_is_skipped_with_a_reason():
    jobs, skipped = resolve(countries_schedule({"NPL": "daily"}, enabled=False))
    assert jobs == []
    assert skipped == ["priority: group disabled"]


def test_expired_job_is_skipped():
    entry = {"frequency": "daily", "expires": date(2026, 8, 4)}
    jobs, skipped = resolve(countries_schedule({"NPL": entry}))
    assert jobs == []
    assert skipped == ["priority/NPL: expired 2026-08-04"]


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


def test_a_job_without_a_frequency_anywhere_is_an_error():
    with pytest.raises(ScheduleError):
        resolve(countries_schedule({"NPL": {"enabled": True}}))


def test_unknown_group_filter_is_an_error():
    with pytest.raises(ScheduleError):
        resolve(countries_schedule({"NPL": "daily"}), group="nope")


def test_group_listed_without_a_block_is_an_error():
    with pytest.raises(ScheduleError):
        resolve({"groups": ["priority", "ghost"], "priority": {"countries": {"NPL": "daily"}}})


def test_group_with_neither_countries_nor_dir_is_an_error():
    with pytest.raises(ScheduleError):
        resolve({"groups": ["priority"], "priority": {"enabled": True}})


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


def test_no_hdx_push_reaches_every_command_line(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    jobs, _, _ = resolve_all(
        countries_schedule({"NPL": "monthly"}), None, None, TODAY, ("--no-hdx-push",)
    )
    assert jobs[0].argv()[-1] == "--no-hdx-push"


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


class StubJob:
    def __init__(self, job_id, argv):
        self.id = job_id
        self._argv = argv

    def argv(self):
        return self._argv


def test_a_failing_job_is_reported_and_the_sweep_continues(capsys):
    jobs = [StubJob("a", ["false"]), StubJob("b", ["true"])]
    assert runner.Runner(30).run(jobs) == ["a"]
    assert "[2/2] b done" in capsys.readouterr().out


def test_a_job_that_overruns_its_timeout_is_a_failure(capsys):
    assert runner.Runner(1).run([StubJob("slow", ["sleep", "5"])]) == ["slow"]
    assert "TIMEOUT" in capsys.readouterr().err


def _gone_within(pid_file: Path, seconds: float) -> bool:
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_timeout_stops_the_process_the_job_started_too(tmp_path):
    pid_file = tmp_path / "inner.pid"
    job = StubJob("slow", ["sh", "-c", f"sleep 300 & echo $! > {pid_file}; wait"])
    assert runner.Runner(2).run([job]) == ["slow"]
    assert _gone_within(pid_file, seconds=2)


def test_an_inner_process_ignoring_sigterm_is_killed_after_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STOP_GRACE_SECONDS", 1)
    pid_file = tmp_path / "inner.pid"
    job = StubJob("stubborn", ["sh", "-c", f"trap '' TERM; sleep 300 & echo $! > {pid_file}; wait"])
    assert runner.Runner(2).run([job]) == ["stubborn"]
    assert _gone_within(pid_file, seconds=2)


def test_a_timed_out_job_gets_sigterm_and_time_to_shut_down(tmp_path):
    marker = tmp_path / "stopped"
    job = StubJob("slow", ["sh", "-c", f"trap 'touch {marker}; exit 0' TERM; sleep 300 & wait"])
    assert runner.Runner(1).run([job]) == ["slow"]
    assert marker.exists()


def test_ctrl_c_with_parallel_workers_starts_no_queued_job(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    started = [tmp_path / "a", tmp_path / "b"]
    late = tmp_path / "late"
    jobs = [
        *[StubJob(f.name, ["sh", "-c", f"touch {f}; exec sleep 1"]) for f in started],
        StubJob("late", ["touch", str(late)]),
    ]

    def interrupt_once_both_run():
        deadline = time.monotonic() + 10
        while not all(f.exists() for f in started) and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_once_both_run, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        runner.Runner(60).run(jobs, 2)
    time.sleep(2)
    assert not late.exists()


def test_a_second_sweep_cannot_take_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    held = runner.acquire_lock()
    assert held is not None
    assert runner.acquire_lock() is None
    held.close()
    assert runner.acquire_lock() is not None


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
    seen = main_with(monkeypatch, tmp_path, ["--no-resume"], countries_schedule({"NPL": "monthly"}))
    assert seen["jobs"][0].argv()[-1] == "--no-resume"


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

    def observe(self, job, log_dir):
        nonlocal peak, running
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        return None

    monkeypatch.setattr(runner.Runner, "run_one", observe)
    runner.Runner(30).run([StubJob(f"j{i}", ["true"]) for i in range(8)], 3)
    assert peak == 3, f"peak concurrency was {peak}, expected 3"


def test_one_failing_job_does_not_stop_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    jobs = [StubJob("bad", ["false"]), StubJob("good", ["true"]), StubJob("bad2", ["false"])]
    assert sorted(runner.Runner(30).run(jobs, 2)) == ["bad", "bad2"]


def test_a_single_worker_writes_no_capture_files(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    assert runner.Runner(30).run([StubJob("a", ["true"])], 1) == []
    assert not (tmp_path / "logs").exists()


def test_parallel_workers_capture_each_job_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    runner.Runner(30).run([StubJob("x/y", ["echo", "hello"])], 2)
    assert (tmp_path / "logs" / "x_y.log").read_text().strip() == "hello"


def test_the_memory_budget_is_divided_between_workers(monkeypatch):
    monkeypatch.setattr(schedule_module, "adaptive_parallel_resources", lambda: (1, 38))
    assert schedule_module.memory_gb_per_worker(1) == 38
    assert schedule_module.memory_gb_per_worker(4) == 9


def test_the_budget_never_drops_below_one_gb(monkeypatch):
    monkeypatch.setattr(schedule_module, "adaptive_parallel_resources", lambda: (1, 1))
    assert schedule_module.memory_gb_per_worker(8) == 1


def test_the_budget_reaches_the_merged_config(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    merged = yaml.safe_load(
        schedule_module.country_config("SDN", "monthly", 7).read_text(encoding="utf-8")
    )
    assert merged["parallel"]["memory_gb"] == 7


def test_no_budget_leaves_the_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    merged = yaml.safe_load(
        schedule_module.country_config("SDN", "monthly").read_text(encoding="utf-8")
    )
    assert "memory_gb" not in (merged.get("parallel") or {})


def test_an_unrunnable_job_does_not_discard_its_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    jobs = [StubJob("missing", ["/nonexistent/binary"]), StubJob("fine", ["true"])]
    assert runner.Runner(30).run(jobs, 2) == ["missing"]


def test_a_failure_names_the_log_that_holds_its_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    runner.Runner(30).run([StubJob("bad", ["false"])], 2)
    assert "bad.log" in capsys.readouterr().err


def test_an_unwritable_log_directory_fails_one_job_not_the_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs").chmod(0o500)
    try:
        jobs = [StubJob("a", ["true"]), StubJob("b", ["true"])]
        assert sorted(runner.Runner(30).run(jobs, 2)) == ["a", "b"]
    finally:
        (tmp_path / "logs").chmod(0o700)


def main_with(monkeypatch, tmp_path, argv, schedule):
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(yaml.safe_dump(schedule), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "acquire_lock", lambda: object())
    monkeypatch.setattr(sys, "argv", ["sweep", *argv])
    seen = {}

    def record(self, batch, workers=1):
        seen.update(jobs=batch, timeout=self.timeout, workers=workers)
        return []

    monkeypatch.setattr(runner.Runner, "run", record)
    assert sweep.main() == 0
    return seen


def test_only_narrows_the_run_to_the_named_countries(monkeypatch, tmp_path):
    seen = main_with(
        monkeypatch,
        tmp_path,
        ["--only", "NPL"],
        countries_schedule({"NPL": "monthly", "HTI": "monthly", "CHL": "monthly"}),
    )
    assert [job.iso3 for job in seen["jobs"]] == ["NPL"]


def test_workers_reaches_the_runner(monkeypatch, tmp_path):
    seen = main_with(
        monkeypatch, tmp_path, ["--workers", "3"], countries_schedule({"NPL": "monthly"})
    )
    assert seen["workers"] == 3


def test_more_than_one_worker_gives_each_country_a_memory_budget(monkeypatch, tmp_path):
    seen = main_with(
        monkeypatch, tmp_path, ["--workers", "4"], countries_schedule({"NPL": "monthly"})
    )
    merged = yaml.safe_load(Path(seen["jobs"][0].config).read_text(encoding="utf-8"))
    assert merged["parallel"]["memory_gb"] == schedule_module.memory_gb_per_worker(4)


def test_a_listed_country_takes_the_group_frequency(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path)
    schedule = {
        "groups": ["high_income"],
        "high_income": {"frequency": "quarterly", "countries": ["AUT", "BEL"]},
    }
    jobs, _ = resolve(schedule)
    assert [(job.iso3, job.frequency) for job in jobs] == [
        ("AUT", "quarterly"),
        ("BEL", "quarterly"),
    ]


def test_a_country_frequency_overrides_the_group(monkeypatch, tmp_path):
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


def test_an_unknown_frequency_is_a_schedule_error():
    with pytest.raises(ScheduleError, match="sometimes"):
        due_module.is_due(_job("sometimes"), None, NOW)


def test_due_runs_each_group_in_order_with_its_own_workers(monkeypatch, tmp_path):
    schedule = {
        "groups": ["priority", "normal"],
        "priority": {"frequency": "monthly", "countries": ["SDN"]},
        "normal": {"frequency": "monthly", "workers": 2, "countries": ["NPL", "BTN"]},
    }
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(yaml.safe_dump(schedule), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(
        due_module,
        "last_publish",
        lambda job: datetime.now(UTC) if job.iso3 == "BTN" else None,
    )
    monkeypatch.setattr(runner, "acquire_lock", lambda: object())
    monkeypatch.setattr(sys, "argv", ["sweep", "--due"])
    calls = []
    monkeypatch.setattr(
        runner.Runner,
        "run",
        lambda self, batch, workers=1: calls.append(([j.iso3 for j in batch], workers)) or [],
    )
    assert sweep.main() == 0
    assert calls == [(["SDN"], 1), (["NPL"], 2)]


def test_a_daily_job_that_finished_after_yesterdays_tick_is_due_today():
    assert due_module.is_due(_job("daily"), NOW - timedelta(hours=23, minutes=40), NOW)


def test_a_numeric_frequency_is_a_schedule_error():
    with pytest.raises(ScheduleError, match="not an HDX frequency"):
        due_module.is_due(_job("30"), None, NOW)


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
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(yaml.safe_dump(schedule), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(runner, "acquire_lock", lambda: object())
    monkeypatch.setattr(sys, "argv", ["sweep"])
    ran = []

    def record(self, batch, workers=1):
        ran.extend(job.id for job in batch)
        return []

    monkeypatch.setattr(runner.Runner, "run", record)
    assert sweep.main() == 1
    assert ran == ["priority/NPL", "events/NPL-FL-2026-b"]
    assert "broken events/NPL-FL-2026-a" in capsys.readouterr().err


def test_a_config_due_cannot_load_makes_the_sweep_fail_after_the_rest(
    tmp_path, monkeypatch, capsys
):
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(yaml.safe_dump(countries_schedule({"NPL": "daily"})), "utf-8")
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(jobs_module, "COUNTRY_CONFIG_DIR", tmp_path / "countries")
    monkeypatch.setattr(jobs_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "acquire_lock", lambda: object())
    monkeypatch.setattr(sys, "argv", ["sweep", "--due"])
    monkeypatch.setattr(due_module, "due_jobs", lambda jobs, now: (jobs, ["events/x: bad key"]))
    ran = []
    monkeypatch.setattr(
        runner.Runner, "run", lambda self, batch, workers=1: ran.extend(batch) or []
    )
    assert sweep.main() == 1
    assert [job.id for job in ran] == ["priority/NPL"]
    assert "broken events/x: bad key" in capsys.readouterr().err


@pytest.fixture
def pushing_base(event_base, tmp_path):
    (tmp_path / "configs" / "_event-base.yaml").write_text(
        "hdx:\n  push: true\n  site: stage\n  combine: true\n"
        "source:\n  overture:\n    enabled: false\n",
        encoding="utf-8",
    )
    return event_base


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


def test_an_event_closed_once_is_not_closed_again(events_dir, pushing_base):
    expired_event(events_dir)
    marker = jobs_module.WORK_DIR / "closed" / "events" / "NPL-FL-2026-a@2026-08-04"
    marker.parent.mkdir(parents=True)
    marker.touch()
    jobs, skipped = resolve({"groups": ["events"], "events": pushing_base})
    assert jobs == []
    assert skipped == ["events/NPL-FL-2026-a: expired 2026-08-04, closed on HDX"]


def test_an_event_closed_before_its_expiry_moved_is_closed_again(events_dir, pushing_base):
    schedule = {"groups": ["events"], "events": pushing_base}
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-08-01\n")
    [first] = resolve(schedule)[0]
    first.closes.parent.mkdir(parents=True)
    first.closes.touch()
    write_config(events_dir, "NPL-FL-2026-a.yaml", "frequency: daily\nexpires: 2026-08-04\n")
    jobs, _ = resolve(schedule)
    assert [job.id for job in jobs] == ["events/NPL-FL-2026-a:close"]


@pytest.mark.parametrize(("frequency", "extra"), [("daily", ("--no-hdx-push",)), ("as needed", ())])
def test_an_expired_event_is_not_closed_without_pushing_or_when_already_as_needed(
    events_dir, pushing_base, frequency, extra
):
    expired_event(events_dir, frequency)
    schedule = {"groups": ["events"], "events": pushing_base}
    jobs, _, _ = resolve_all(schedule, None, None, TODAY, extra)
    assert jobs == []


def test_a_closing_job_is_due_without_reading_the_catalog(tmp_path, monkeypatch):
    def unreachable(job):
        raise AssertionError(job.id)

    monkeypatch.setattr(due_module, "last_publish", unreachable)
    job = Job("events/a:close", "events", "metadata", tmp_path, None, closes=tmp_path / "m")
    assert due_module.due_jobs([job], NOW) == ([job], [])


@pytest.mark.parametrize(
    ("failed", "closed"), [([], True), (["events/NPL-FL-2026-a:close"], False)]
)
def test_only_a_closing_job_that_succeeds_marks_the_event_closed(
    events_dir, pushing_base, tmp_path, monkeypatch, failed, closed
):
    expired_event(events_dir)
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(
        yaml.safe_dump({"groups": ["events"], "events": pushing_base}), encoding="utf-8"
    )
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(runner, "acquire_lock", lambda: object())
    monkeypatch.setattr(sys, "argv", ["sweep"])
    monkeypatch.setattr(runner.Runner, "run", lambda self, batch, workers=1: failed)
    sweep.main()
    marker = jobs_module.WORK_DIR / "closed" / "events" / "NPL-FL-2026-a@2026-08-04"
    assert marker.exists() is closed


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
    schedule_file = tmp_path / "schedule.yaml"
    schedule_file.write_text(yaml.safe_dump(schedule), encoding="utf-8")
    monkeypatch.setattr(jobs_module, "SCHEDULE_FILE", schedule_file)
    monkeypatch.setattr(sys, "argv", ["sweep", "--check"])
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
    close = Job("events/a:close", "events", "metadata", tmp_path, None, closes=tmp_path / "m")
    later = Job("tm/1", "tm", "osm", tmp_path, None, frequency="daily")
    assert due_module.due_jobs([close, later], NOW) == ([close, later], [])


def test_an_event_without_its_own_frequency_publishes_the_groups(events_dir, event_base):
    write_config(events_dir, "NPL-FL-2026-a.yaml", "source:\n  osm:\n    enabled: true\n")
    group = {**event_base, "frequency": "weekly"}
    [job], _ = resolve({"groups": ["events"], "events": group})
    assert yaml.safe_load(job.config.read_text(encoding="utf-8"))["frequency"] == "weekly"
