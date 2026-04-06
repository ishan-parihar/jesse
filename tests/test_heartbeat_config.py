"""Tests for HeartbeatConfig — duration parsing, active hours, IANA timezone."""

from jesse.heartbeat_config import (
    HeartbeatConfig,
    parse_duration,
    is_within_active_hours,
    resolve_heartbeat_config,
    HEARTBEAT_DEFAULT_PROMPT,
)


def test_parse_duration_seconds():
    assert parse_duration("30s") == 30.0
    assert parse_duration("60s") == 60.0
    assert parse_duration("90s") == 90.0


def test_parse_duration_minutes():
    assert parse_duration("1m") == 60.0
    assert parse_duration("5m") == 300.0
    assert parse_duration("30m") == 1800.0
    assert parse_duration("60m") == 3600.0


def test_parse_duration_hours():
    assert parse_duration("1h") == 3600.0
    assert parse_duration("2h") == 7200.0


def test_parse_duration_combined():
    assert parse_duration("2h30m") == 9000.0
    assert parse_duration("1d") == 86400.0


def test_parse_duration_invalid():
    try:
        parse_duration("xyz")
        assert False, "Should have raised"
    except ValueError:
        pass


def test_parse_duration_empty():
    try:
        parse_duration("")
        assert False, "Should have raised"
    except ValueError:
        pass


def test_config_default():
    cfg = HeartbeatConfig()
    assert cfg.enabled is True
    assert cfg.every_seconds == 1800.0  # "30m"
    assert cfg.target == "none"
    assert cfg.show_alerts is True
    assert cfg.show_ok is False


def test_config_with_every_override():
    cfg = HeartbeatConfig(every="1h")
    assert cfg.every_seconds == 3600.0


def test_config_target_telegram():
    cfg = HeartbeatConfig(target="telegram")
    assert cfg.target == "telegram"


def test_active_hours_no_restriction():
    cfg = HeartbeatConfig()  # no active_hours_start/end
    assert is_within_active_hours(cfg) is True


def test_active_hours_normal_window():
    cfg = HeartbeatConfig(
        active_hours_start="08:00", active_hours_end="22:00", timezone="UTC"
    )
    from datetime import datetime, timezone as tz

    inside = datetime(2025, 4, 7, 12, 0, tzinfo=tz.utc)  # noon UTC
    outside = datetime(2025, 4, 7, 3, 0, tzinfo=tz.utc)  # 3am UTC
    assert is_within_active_hours(cfg, now=inside) is True
    assert is_within_active_hours(cfg, now=outside) is False


def test_active_hours_overnight_window():
    cfg = HeartbeatConfig(
        active_hours_start="22:00", active_hours_end="06:00", timezone="UTC"
    )
    from datetime import datetime, timezone as tz

    inside = datetime(2025, 4, 7, 23, 0, tzinfo=tz.utc)  # 11pm UTC
    inside2 = datetime(2025, 4, 8, 2, 0, tzinfo=tz.utc)  # 2am UTC
    outside = datetime(2025, 4, 7, 14, 0, tzinfo=tz.utc)  # 2pm UTC
    assert is_within_active_hours(cfg, now=inside) is True
    assert is_within_active_hours(cfg, now=inside2) is True
    assert is_within_active_hours(cfg, now=outside) is False


def test_active_hours_zero_width():
    cfg = HeartbeatConfig(
        active_hours_start="12:00", active_hours_end="12:00", timezone="UTC"
    )
    from datetime import datetime, timezone as tz

    assert (
        is_within_active_hours(cfg, now=datetime(2025, 4, 7, 12, 0, tzinfo=tz.utc))
        is False
    )


def test_active_hours_with_timezone():
    cfg = HeartbeatConfig(
        active_hours_start="09:00", active_hours_end="17:00", timezone="Asia/Calcutta"
    )
    from datetime import datetime, timezone as tz

    noon_utc = datetime(2025, 4, 7, 12, 0, tzinfo=tz.utc)  # 5:30pm IST — outside
    morning_utc = datetime(2025, 4, 7, 4, 0, tzinfo=tz.utc)
    assert is_within_active_hours(cfg, now=noon_utc) is False
    assert is_within_active_hours(cfg, now=morning_utc) is True


def test_resolve_heartbeat_config():
    cfg = resolve_heartbeat_config()
    assert isinstance(cfg, HeartbeatConfig)


def test_resolve_with_overrides():
    cfg = resolve_heartbeat_config({"every": "1h", "enabled": False})
    assert cfg.every_seconds == 3600.0
    assert cfg.enabled is False


def test_config_effective_prompt():
    cfg = HeartbeatConfig(prompt="custom prompt here")
    assert cfg.effective_prompt == "custom prompt here"


def test_config_effective_prompt_default():
    cfg = HeartbeatConfig()
    assert cfg.effective_prompt == HEARTBEAT_DEFAULT_PROMPT


def test_config_with_overrides_method():
    cfg = HeartbeatConfig().with_overrides(every="10m", target="last")
    assert cfg.every_seconds == 600.0
    assert cfg.target == "last"


def test_config_light_context():
    cfg = HeartbeatConfig(light_context=True)
    assert cfg.light_context is True


def test_config_ack_max_chars():
    cfg = HeartbeatConfig(ack_max_chars=500)
    assert cfg.ack_max_chars == 500
