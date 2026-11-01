from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from config import DEFAULTS, ConfigError, Settings


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_missing_file_is_created_with_the_defaults(tmp_path):
    path = tmp_path / "config.json"
    settings = Settings(path)
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULTS
    assert settings.product == "SST" and settings.port == 8080 and settings.host == "127.0.0.1"


def test_the_shipped_config_is_valid():
    from config import DEFAULT_CONFIG_PATH

    settings = Settings(DEFAULT_CONFIG_PATH)
    assert settings.product in ("SST", "HST") and settings.database_path.name == "queue.db"


def test_the_shipped_config_uses_the_alarm_map_inside_the_project():
    from config import DEFAULT_CONFIG_PATH, HERE

    settings = Settings(DEFAULT_CONFIG_PATH)
    alarm_map = Path(settings.alarm_map)
    assert alarm_map.is_file() and alarm_map.is_relative_to(HERE)
    assert not re.match(r"^[A-Za-z]:|^[/\\]", settings.alarm_map_raw)  # written relative: no drive letter, no root


def test_the_alarm_map_that_ships_with_the_app_loads():
    from config import DEFAULT_CONFIG_PATH
    from pcs_die_file import pcs_die_file_checker as rc

    amap = rc.AlarmIdMap.load(Settings(DEFAULT_CONFIG_PATH).alarm_map)
    assert amap is not None
    assert amap.parts["Rule"].get("XofY") and amap.parts["Action"] and amap.parts["BinOrToken"]


def test_the_default_alarm_map_is_relative_to_the_config_folder(tmp_path):
    settings = Settings(tmp_path / "config.json")
    assert settings.alarm_map_raw == "DpcConfig/V1.00/AlarmIdMapping.xml"
    assert settings.alarm_map == str(tmp_path / "DpcConfig" / "V1.00" / "AlarmIdMapping.xml")
    assert Settings(write(tmp_path / "c2.json", {"paths": {"alarm_map": ""}})).alarm_map == ""  # "" = none at all


def test_there_is_no_browser_setting(tmp_path):
    assert "open_browser" not in DEFAULTS["server"]
    settings = Settings(write(tmp_path / "config.json", {"server": {"open_browser": True}}))  # an old file still loads
    assert not hasattr(settings, "open_browser")


def test_update_can_set_the_api_token(tmp_path):
    path = write(tmp_path / "config.json", {"ui": {"product": "HST"}})
    settings = Settings(path)
    settings.update(api_token="  abc_DEF-123  ")
    assert settings.api_token == "abc_DEF-123"
    assert Settings(path).api_token == "abc_DEF-123" and json.loads(path.read_text())["ui"]["product"] == "HST"
    for bad in ("tøken", "to\nken"):
        with pytest.raises(ConfigError, match="ASCII"):
            settings.update(api_token=bad)
    assert Settings(path).api_token == "abc_DEF-123"  # a refused token changes nothing
    settings.update(api_token="")
    assert Settings(path).api_token == ""


def test_missing_keys_fall_back_to_the_defaults(tmp_path):
    settings = Settings(write(tmp_path / "config.json", {"server": {"port": 9000}, "tasks": {"ui_refresh_s": 2}}))
    assert settings.port == 9000 and settings.host == "127.0.0.1"
    assert settings.ui_refresh == 2.0
    assert settings.monitor_interval == 60.0
    assert settings.worker_offline_after == timedelta(seconds=60)
    assert settings.assigned_timeout == timedelta(minutes=5)
    assert settings.running_timeout == timedelta(hours=1)
    assert settings.api_token == ""


def test_database_path_is_relative_to_the_config_file(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    settings = Settings(write(folder / "config.json", {"database": {"path": "data/q.db"}}))
    assert settings.database_path == folder / "data" / "q.db"
    absolute = tmp_path / "elsewhere" / "q.db"
    settings = Settings(write(folder / "config.json", {"database": {"path": str(absolute)}}))
    assert settings.database_path == absolute


def test_api_token_is_trimmed(tmp_path):
    assert Settings(write(tmp_path / "c.json", {"tasks": {"api_token": "  s3cret "}})).api_token == "s3cret"


def test_public_url_is_trimmed_and_may_be_empty(tmp_path):
    assert Settings(write(tmp_path / "a.json", {"server": {"public_url": "  http://pc1.example.com/ "}})).public_url == "http://pc1.example.com"
    assert Settings(write(tmp_path / "b.json", {"server": {"public_url": "https://pc1.example.com:8443"}})).public_url == "https://pc1.example.com:8443"
    assert Settings(write(tmp_path / "c.json", {})).public_url == "" == DEFAULTS["server"]["public_url"]


def test_the_browsers_use_polling_unless_websocket_is_switched_on(tmp_path):
    assert Settings(write(tmp_path / "a.json", {})).websocket is False  # also a config.json written before the setting existed
    assert Settings(write(tmp_path / "b.json", {"server": {"websocket": True}})).websocket is True
    assert Settings(write(tmp_path / "c.json", {"server": {"websocket": False}})).websocket is False
    with pytest.raises(ConfigError, match="server.websocket"):
        Settings(write(tmp_path / "d.json", {"server": {"websocket": "yes"}}))


def test_the_app_compresses_nothing_unless_asked(tmp_path):
    assert Settings(write(tmp_path / "a.json", {})).compression is False  # also a config.json written before the setting existed
    assert Settings(write(tmp_path / "b.json", {"server": {"compression": True}})).compression is True
    with pytest.raises(ConfigError, match="server.compression"):
        Settings(write(tmp_path / "c.json", {"server": {"compression": "gzip"}}))


def test_the_shipped_config_can_be_reached_by_the_workers():
    """Listening on this PC only is right behind a reverse proxy (IIS) - but then the config has to say where the proxy
    publishes the app, or the package for PC2 has no address to connect to."""
    from config import DEFAULT_CONFIG_PATH

    settings = Settings(DEFAULT_CONFIG_PATH)
    assert settings.host not in ("127.0.0.1", "localhost", "::1") or settings.public_url


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        ({"database": {"path": ""}}, "database.path"),
        ({"database": {"path": 5}}, "database.path"),
        ({"tasks": {"monitor_interval_s": 0}}, "tasks.monitor_interval_s"),
        ({"tasks": {"worker_offline_after_s": -1}}, "tasks.worker_offline_after_s"),
        ({"tasks": {"assigned_timeout_s": "300"}}, "tasks.assigned_timeout_s"),
        ({"tasks": {"running_timeout_s": True}}, "tasks.running_timeout_s"),
        ({"tasks": {"ui_refresh_s": float("nan")}}, "tasks.ui_refresh_s"),
        ({"tasks": {"api_token": 123}}, "tasks.api_token"),
        ({"tasks": {"api_token": "tøken"}}, "tasks.api_token"),
        ({"tasks": {"api_token": "to\nken"}}, "tasks.api_token"),
        ({"ui": {"product": "XST"}}, "ui.product"),
        ({"server": {"port": 70000}}, "server.port"),
        ({"server": {"public_url": 5}}, "server.public_url"),
        ({"server": {"public_url": "pc1.example.com"}}, "server.public_url"),
        ({"server": {"public_url": "ftp://pc1.example.com"}}, "server.public_url"),
        ({"server": {"public_url": "http://pc1.example.com/pcs"}}, "server.public_url"),
        ({"server": {"public_url": "http://pc1.example.com:99999"}}, "server.public_url"),
        ({"server": {"public_url": "http://pc1.example.com:abc"}}, "server.public_url"),
        ({"server": {"public_url": "http://user:pw@pc1.example.com"}}, "server.public_url"),
        ({"server": {"public_url": "http://pc1.example.com?x=1"}}, "server.public_url"),
        ({"tasks": []}, '"tasks" must be an object'),
    ],
)
def test_bad_values_are_refused_with_the_key_name(tmp_path, data, needle):
    with pytest.raises(ConfigError, match=needle):
        Settings(write(tmp_path / "config.json", data))


def test_broken_json_says_where(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"paths": {"alarm_map": "C:\\x"}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="line 1"):
        Settings(path)


def test_unknown_keys_survive_a_save(tmp_path):
    path = write(tmp_path / "config.json", {"extra": {"keep": 1}, "ui": {"product": "SST"}})
    settings = Settings(path)
    settings.data["extra"] = {"keep": 1}
    settings.update(product="hst")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["extra"] == {"keep": 1} and saved["ui"]["product"] == "HST"
    assert saved["tasks"] == DEFAULTS["tasks"] and saved["database"] == DEFAULTS["database"]


def test_update_keeps_a_token_that_was_rotated_while_the_app_runs(tmp_path):
    """``build_worker_package.py --rotate-token`` is another process: it writes the new token into config.json while
    the web app has the old one in memory.  Saving the Product box afterwards must not bring the old token back."""
    path = write(tmp_path / "config.json", {"tasks": {"api_token": "old-token"}})
    app = Settings(path)
    Settings(path).update(api_token="new-token")  # the package builder

    app.update(product="hst")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tasks"]["api_token"] == "new-token" and saved["ui"]["product"] == "HST"


def test_update_keeps_a_hand_edit_made_since_the_file_was_read(tmp_path):
    path = write(tmp_path / "config.json", {"paths": {"alarm_map": "a.xml"}})
    settings = Settings(path)
    write(path, {"paths": {"alarm_map": "edited-by-hand.xml"}, "extra": {"keep": 1}})

    settings.update(product="HST")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["paths"]["alarm_map"] == "edited-by-hand.xml" and saved["extra"] == {"keep": 1}
    assert saved["ui"]["product"] == "HST"
    assert settings.alarm_map_raw == "edited-by-hand.xml"


def test_update_leaves_a_file_that_is_broken_right_now_alone(tmp_path):
    path = write(tmp_path / "config.json", {"tasks": {"api_token": "keep-me"}})
    settings = Settings(path)
    path.write_text("{ half-typed", encoding="utf-8")

    with pytest.raises(ConfigError, match="line 1"):
        settings.update(product="HST")

    assert path.read_text(encoding="utf-8") == "{ half-typed"  # the hand edit in progress is not overwritten


def test_update_refuses_a_wrong_value_and_changes_nothing(tmp_path):
    path = write(tmp_path / "config.json", {"ui": {"product": "SST"}})
    settings = Settings(path)
    with pytest.raises(ConfigError, match="ui.product"):
        settings.update(product="XST")
    assert settings.product == "SST" and Settings(path).product == "SST"
    settings.update(product="hst")  # (and the file is still usable afterwards)
    assert Settings(path).product == "HST"


def test_updates_from_many_threads_lose_nothing_and_leave_no_temp_files(tmp_path):
    path = write(tmp_path / "config.json", {})
    first, second = Settings(path), Settings(path)  # two owners of one file
    errors: list[BaseException] = []

    def run(settings: Settings, make) -> None:
        try:
            for i in range(25):
                settings.update(**make(i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(first, lambda i: {"product": ("HST", "SST")[i % 2]})),
        threading.Thread(target=run, args=(second, lambda i: {"alarm_map": f"map{i}.xml"})),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert not errors
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ui"]["product"] == "HST" and saved["paths"]["alarm_map"] == "map24.xml"  # the last of each
    assert list(tmp_path.glob("*.tmp")) == []


def test_saving_tries_again_when_windows_holds_the_file_for_a_moment(tmp_path, monkeypatch):
    path = write(tmp_path / "config.json", {})
    settings = Settings(path)
    real_replace, calls = os.replace, []

    def busy_twice(src, dst):
        calls.append(src)
        if len(calls) <= 2:
            raise PermissionError(13, "the file is in use")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", busy_twice)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    settings.update(product="HST")
    assert len(calls) == 3 and Settings(path).product == "HST" and list(tmp_path.glob("*.tmp")) == []


def test_a_save_that_cannot_be_finished_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = write(tmp_path / "config.json", {"ui": {"product": "SST"}})
    settings = Settings(path)

    def locked(src, dst):
        raise PermissionError(13, "the file is in use")

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(PermissionError):
        settings.update(product="HST")
    assert json.loads(path.read_text(encoding="utf-8"))["ui"]["product"] == "SST"
    assert list(tmp_path.glob("*.tmp")) == []


def test_hot_reload(tmp_path):
    path = write(tmp_path / "config.json", {"checker": {"skip_workflow_check": True}})
    settings = Settings(path)
    assert not settings.reload_if_changed()
    write(path, {"checker": {"skip_workflow_check": False}})
    stamp = path.stat().st_mtime_ns
    os.utime(path, ns=(stamp + 5_000_000_000, stamp + 5_000_000_000))  # (two writes can share one timestamp tick)
    assert settings.reload_if_changed() and settings.skip_workflow_check is False
    path.write_text("{ nope", encoding="utf-8")
    os.utime(path, ns=(stamp + 10_000_000_000, stamp + 10_000_000_000))
    with pytest.raises(ConfigError):
        settings.reload_if_changed()
    assert settings.skip_workflow_check is False  # the old values stay
