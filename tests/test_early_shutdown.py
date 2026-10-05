"""Selected loads use a separate outage timer without triggering the whole estate."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app import engine as engine_mod
from app.config import AppConfig, PbsHostConfig, PveHostConfig, SnmpConfig, Thresholds, load_config, save_config
from app.engine import Engine
from app.ups import UpsState


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "STATE_PATH", tmp_path / "engine-state.json")
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(engine_mod, "_now", lambda: now[0])
    monkeypatch.setattr(Engine, "_emit", AsyncMock())
    send = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(engine_mod.targets, "shutdown", send)
    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="a", host="ups-a"), SnmpConfig(id="b", host="ups-b")],
        hosts=[
            PveHostConfig(name="early", api_url="https://early", ups_ids=["a"],
                          shutdown_on_power_loss=True),
            PveHostConfig(name="normal", api_url="https://normal", ups_ids=["a"]),
            PveHostConfig(name="self", api_url="https://self", ups_ids=["a"],
                          this_host=True, shutdown_on_power_loss=True),
        ],
        thresholds=Thresholds(on_battery_seconds=600, early_shutdown_seconds=30,
                              runtime_below_minutes=None, charge_below_percent=None,
                              on_battery_low=False),
    )
    eng = Engine(cfg)
    for rt in eng.ups_rt.values():
        rt.state = UpsState(reachable=True, power_source="normal")
    return eng, now, send


def battery(eng, uid="a"):
    eng.ups_rt[uid].state = UpsState(reachable=True, power_source="battery")


async def test_early_timer_then_normal_shutdown_and_self_last(rig):
    eng, now, send = rig
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=29)
    await eng._evaluate()
    send.assert_not_called()
    now[0] += timedelta(seconds=1)
    await eng._evaluate()
    assert [c.args[0].name for c in send.call_args_list] == ["early"]
    assert not eng.ups_rt["a"].triggered
    await eng._evaluate()
    assert send.call_count == 1
    now[0] += timedelta(seconds=570)
    await eng._evaluate()
    assert [c.args[0].name for c in send.call_args_list] == ["early", "normal", "self"]


async def test_short_outage_resets_the_timer(rig):
    eng, now, send = rig
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=29)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="normal")
    await eng._evaluate()
    now[0] += timedelta(seconds=100)
    battery(eng)
    await eng._evaluate()
    send.assert_not_called()
    now[0] += timedelta(seconds=30)
    await eng._evaluate()
    assert send.call_count == 1


@pytest.mark.parametrize("policy,expected", [("all", 0), ("any", 1)])
async def test_early_shutdown_respects_redundant_feeds(rig, policy, expected):
    eng, now, send = rig
    eng.cfg.hosts[0].ups_ids = ["a", "b"]
    eng.cfg.hosts[0].ups_policy = policy
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=30)
    await eng._evaluate()
    assert send.call_count == expected
    battery(eng, "b")
    await eng._evaluate()
    assert send.call_count == expected
    now[0] += timedelta(seconds=30)
    await eng._evaluate()
    assert send.call_count == 1


@pytest.mark.parametrize("unconfirmed", [False, True])
async def test_unknown_power_never_fires_early_timer(rig, unconfirmed):
    eng, now, send = rig
    rt = eng.ups_rt["a"]
    rt.on_battery_since = now[0] - timedelta(seconds=60)
    rt.restored_unconfirmed = unconfirmed
    rt.state = UpsState(reachable=False, power_source="battery")
    await eng._evaluate()
    send.assert_not_called()


async def test_custom_delay_dry_run_and_recovery(rig):
    eng, now, send = rig
    eng.cfg.dry_run = True
    eng.cfg.thresholds.early_shutdown_seconds = 90
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=30)
    await eng._evaluate()
    assert not any(eng.host_fired.values())
    now[0] += timedelta(seconds=60)
    await eng._evaluate()
    assert eng.host_fired[eng.cfg.hosts[0].key]
    send.assert_not_called()
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="normal")
    await eng._evaluate()
    assert not any(eng.host_fired.values())


async def test_early_cluster_node_does_not_prepare_or_stop_other_nodes(rig, monkeypatch):
    eng, now, send = rig
    eng.cfg.hosts[0].cluster = True
    prepare = AsyncMock(side_effect=lambda hosts: hosts)
    monkeypatch.setattr(eng, "_prepare_clusters", prepare)
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=30)
    await eng._evaluate()
    prepare.assert_not_called()
    assert [c.args[0].name for c in send.call_args_list] == ["early"]
    now[0] += timedelta(seconds=570)
    await eng._evaluate()
    prepare.assert_awaited_once()


async def test_early_failure_retries_and_disabled_host_is_ignored(rig):
    eng, now, send = rig
    send.side_effect = [(False, "retry"), (True, "ok")]
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=30)
    eng.cfg.hosts[0].enabled = False
    await eng._evaluate()
    send.assert_not_called()
    eng.cfg.hosts[0].enabled = True
    await eng._evaluate()
    await eng._evaluate()
    await eng._evaluate()
    assert send.call_count == 2


async def test_regular_threshold_does_not_wait_for_early_timer(rig):
    eng, now, send = rig
    eng.cfg.thresholds.on_battery_seconds = 10
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=10)
    await eng._evaluate()
    assert {c.args[0].name for c in send.call_args_list} == {"early", "normal", "self"}


@pytest.mark.parametrize("host_type", [PveHostConfig, PbsHostConfig])
def test_new_settings_survive_config_roundtrip(tmp_path, host_type):
    cfg = AppConfig(hosts=[host_type(name="load", api_url="https://load",
                                   shutdown_on_power_loss=True)],
                    thresholds=Thresholds(early_shutdown_seconds=75))
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    restored = load_config(path)
    assert restored.hosts[0].shutdown_on_power_loss is True
    assert restored.thresholds.early_shutdown_seconds == 75
    assert host_type(name="old", api_url="x").shutdown_on_power_loss is False
    assert Thresholds().early_shutdown_seconds == 30


async def test_preview_explains_early_timer_without_sending(rig):
    eng, now, send = rig
    message = await eng.simulate_shutdown()
    assert "Early load shedding after 30s" in message
    assert "early (PVE)" in message
    assert not eng.host_fired
    send.assert_not_called()


async def test_early_load_order_and_pbs_target(rig):
    eng, now, send = rig
    eng.cfg.hosts[0].order = 3
    eng.cfg.hosts.append(PbsHostConfig(
        name="backup", api_url="https://backup", ups_ids=["a"], order=1,
        shutdown_on_power_loss=True,
    ))
    battery(eng)
    await eng._evaluate()
    now[0] += timedelta(seconds=30)
    await eng._evaluate()
    assert [c.args[0].name for c in send.call_args_list] == ["backup", "early"]


def test_budget_counts_separate_early_stage(rig):
    eng, now, send = rig
    # Early, regular, self, and the retry stage before self.
    assert engine_mod.shutdown_budget(eng.cfg).stages == 4
