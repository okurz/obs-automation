import re
import subprocess
from unittest.mock import MagicMock

import pytest
from rich.table import Table

from obs_automation.main import (
    _get_branch_project,
    _is_transient_failure,
    fetch_anitya_id_by_name_or_url,
    fetch_latest_version,
    get_authenticated_user,
    get_missing_services,
    get_obs_package_url,
    get_user_packages,
    main,
    process_package,
    run_cmd,
    run_obs_readonly,
    state,
)


def _osc_error(stderr: str) -> subprocess.CalledProcessError:
    err = subprocess.CalledProcessError(1, ["osc", "api"])
    err.stdout = ""
    err.stderr = stderr
    return err


def _res(stdout: str) -> MagicMock:
    mock_res = MagicMock()
    mock_res.stdout = stdout
    return mock_res


def test_fetch_latest_version(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"version": "1.2.3"}
    mock_get.return_value = mock_resp

    assert fetch_latest_version("123") == "1.2.3"
    mock_get.assert_called_once_with("https://release-monitoring.org/api/project/123", timeout=10.0)
    mock_resp.raise_for_status.assert_called_once()


def test_fetch_latest_version_empty(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"version": ""}
    mock_get.return_value = mock_resp

    mocker.patch("tenacity.nap.time.sleep")
    with pytest.raises(ValueError, match="Anitya API returned empty version"):
        fetch_latest_version("123")


def test_run_cmd(mocker):
    mock_run = mocker.patch("subprocess.run", return_value="done")
    res = run_cmd(["ls", "-l"], check=False)
    assert res == "done"
    mock_run.assert_called_once_with(["ls", "-l"], check=False, text=True, capture_output=True)


def test_get_branch_project():
    stdout = (
        "A working copy of the branched package can be checked out with:\n\n"
        "osc co home:okurz:branches:Cloud:Tools/cf-cli\n"
    )
    assert _get_branch_project(stdout) == "home:okurz:branches:Cloud:Tools"

    assert _get_branch_project("no match here") is None


def test_get_obs_package_url(mocker):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = (
        """<package name="cf-cli" project="Cloud:Tools"><url>https://github.com/cloudfoundry/cli.git</url></package>"""
    )
    mock_run_cmd.return_value = mock_res

    assert get_obs_package_url("Cloud:Tools", "cf-cli") == "https://github.com/cloudfoundry/cli.git"


def test_get_obs_package_url_missing(mocker):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = """<package name="cf-cli" project="Cloud:Tools"></package>"""
    mock_run_cmd.return_value = mock_res

    assert get_obs_package_url("Cloud:Tools", "cf-cli") is None


def test_fetch_anitya_id_by_name_or_url(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "items": [
            {"id": 999, "homepage": "https://wrong.com"},
            {"id": 888, "ecosystem": None},
            {"id": 385503, "ecosystem": "https://github.com/cloudfoundry/cli"},
        ]
    }
    mock_get.return_value = mock_resp

    assert fetch_anitya_id_by_name_or_url("cf-cli", "https://github.com/cloudfoundry/cli.git") == "385503"


def test_fetch_anitya_id_by_name_or_url_missing(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": []}
    mock_get.return_value = mock_resp

    with pytest.raises(ValueError, match="Could not find Anitya project"):
        fetch_anitya_id_by_name_or_url("cf-cli", "https://github.com/cloudfoundry/cli.git")


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("", "Server returned an error: HTTP Error 503: Service Unavailable", True),
        ("", "Server returned an error: HTTP Error 502: Bad Gateway", True),
        ("", "Server returned an error: HTTP Error 504: Gateway Time-out", True),
        ("", "Server returned an error: HTTP Error 500: Internal Server Error", True),
        ("Server returned an error: HTTP Error 503: Service Unavailable", "", True),
        ("", "urlopen error: Connection refused", True),
        ("", "Remote host closed the connection: Connection reset by peer", True),
        ("", "socket operation timed out", True),
        ("", "Temporary failure in name resolution", True),
        ("", "Server returned an error: HTTP Error 401: Unauthorized", False),
        ("", "Server returned an error: HTTP Error 404: Not Found", False),
        ("", "openSUSE:Factory/x: package 'x' does not exist", False),
        ("", "", False),
    ],
)
def test_is_transient_failure_matches_osc_output(stdout, stderr, expected):
    err = _osc_error(stderr)
    err.stdout = stdout
    assert _is_transient_failure(err) is expected


def test_is_transient_failure_rejects_non_osc_errors():
    assert _is_transient_failure(RuntimeError("HTTP Error 503: Service Unavailable")) is False


def test_run_obs_readonly_passes_kwargs_to_run_cmd(mocker, tmp_path):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    assert run_obs_readonly(["osc", "update"], cwd=str(tmp_path)) is mock_run_cmd.return_value
    mock_run_cmd.assert_called_once_with(["osc", "update"], cwd=str(tmp_path))


@pytest.mark.parametrize(
    ("stderr", "failures", "recovers", "expected_calls", "expected_sleeps"),
    [
        ("Server returned an error: HTTP Error 503: Service Unavailable", 1, True, 2, 1),
        ("Server returned an error: HTTP Error 503: Service Unavailable", 4, False, 4, 3),
        ("Server returned an error: HTTP Error 401: Unauthorized", 1, False, 1, 0),
    ],
)
def test_get_user_packages_retries_transient_osc_failures(
    mocker, stderr, failures, recovers, expected_calls, expected_sleeps
):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_sleep = mocker.patch("tenacity.nap.time.sleep")
    errors = [_osc_error(stderr) for _ in range(failures)]
    mock_run_cmd.side_effect = [
        *errors,
        _res('<collection><package name="cf-cli" project="Cloud:Tools" /></collection>'),
    ]

    if recovers:
        assert get_user_packages("okurz") == [("Cloud:Tools", "cf-cli")]
    else:
        with pytest.raises(subprocess.CalledProcessError):
            get_user_packages("okurz")

    assert mock_run_cmd.call_count == expected_calls
    assert mock_sleep.call_count == expected_sleeps


@pytest.mark.parametrize(
    ("side_effects", "expected_url", "expected_calls"),
    [
        (
            [
                _osc_error("Server returned an error: HTTP Error 503: Service Unavailable"),
                _res(
                    '<package name="cf-cli" project="Cloud:Tools"><url>https://github.com/cloudfoundry/cli.git</url></package>'
                ),
            ],
            "https://github.com/cloudfoundry/cli.git",
            2,
        ),
        ([_osc_error("Server returned an error: HTTP Error 404: Not Found")], None, 1),
        ([_osc_error("Server returned an error: HTTP Error 503: Service Unavailable")] * 4, None, 4),
    ],
)
def test_get_obs_package_url_retries_transient_failures(mocker, side_effects, expected_url, expected_calls):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd", side_effect=side_effects)
    mocker.patch("tenacity.nap.time.sleep")

    assert get_obs_package_url("Cloud:Tools", "cf-cli") == expected_url
    assert mock_run_cmd.call_count == expected_calls


@pytest.mark.parametrize(
    ("input_user", "expected_query_user"),
    [
        ("okurz", "okurz"),
        ("okurz\r", "okurz"),
        ("  okurz  ", "okurz"),
    ],
)
def test_get_user_packages(mocker, input_user, expected_query_user):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.stdout = """<collection><package name="cf-cli" project="Cloud:Tools" /></collection>"""
    mock_run_cmd.return_value = mock_res

    assert get_user_packages(input_user) == [("Cloud:Tools", "cf-cli")]
    mock_run_cmd.assert_called_once_with(
        ["osc", "api", f"/search/package?match=person/@userid='{expected_query_user}'"], capture_output=True
    )


@pytest.mark.parametrize(
    ("env_value", "whois_stdout", "whois_returncode", "expected"),
    [
        ("botuser", None, None, "botuser"),
        ("  botuser\r", None, None, "botuser"),
        ("   ", 'okurz: "Oliver Kurz" <okurz@suse.com>\n', 0, "okurz"),
        (None, 'okurz: "Oliver Kurz" <okurz@suse.com>\n', 0, "okurz"),
        (None, ': "Anonymous"\n', 0, None),
        (None, "no-colon-in-output\n", 0, None),
        (None, "okurz: x\n", 1, None),
    ],
)
def test_get_authenticated_user(mocker, monkeypatch, env_value, whois_stdout, whois_returncode, expected):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.stdout = whois_stdout
    mock_res.returncode = whois_returncode
    mock_run_cmd.return_value = mock_res
    if env_value is None:
        monkeypatch.delenv("OBS_USER", raising=False)
    else:
        monkeypatch.setenv("OBS_USER", env_value)

    assert get_authenticated_user() == expected


def test_process_package_already_up_to_date(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Skipped"


def test_process_package_update_flow(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mock_write_text = mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"

    mock_write_text.assert_called_once_with('<param name="revision">v1.2.4</param>')
    assert any(c.args[0] == ["osc", "ci", "-m", "Update cf-cli to v1.2.4"] for c in mock_run_cmd.call_args_list)


def test_process_package_missing_anitya_id(mocker):
    mocker.patch("obs_automation.main.get_obs_package_url", return_value="url")
    mocker.patch("obs_automation.main.fetch_anitya_id_by_name_or_url", return_value="123")
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")

    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id=None) == "Updated"


def test_main_cli_project_package(mocker):
    mock_process = mocker.patch("obs_automation.main.process_package")
    main(project="P", package="p", anitya_id="123", config=None, user=None)
    mock_process.assert_called_once_with("P", "p", "123", mocker.ANY, mocker.ANY)


def test_main_cli_config(mocker, tmp_path):
    mock_process = mocker.patch("obs_automation.main.process_package")

    config_file = tmp_path / "config.yml"
    config_file.write_text("packages:\n  - project: P\n    package: p\n    anitya_id: 123")

    main(project=None, package=None, anitya_id=None, config=config_file, user=None)
    mock_process.assert_called_once_with("P", "p", "123", mocker.ANY, mocker.ANY)


def test_main_cli_config_error(mocker, tmp_path):
    mock_process = mocker.patch("obs_automation.main.process_package", side_effect=Exception("Failed"))

    config_file = tmp_path / "config.yml"
    config_file.write_text("packages:\n  - project: P\n    package: p")

    main(project=None, package=None, anitya_id=None, config=config_file, user=None)
    mock_process.assert_called_once_with("P", "p", None, mocker.ANY, mocker.ANY)


def test_main_cli_user(mocker):
    mocker.patch("obs_automation.main.get_user_packages", return_value=[("P", "p")])
    mock_process = mocker.patch("obs_automation.main.process_package")

    main(project=None, package=None, anitya_id=None, config=None, user="okurz")
    mock_process.assert_called_once_with("P", "p", None, mocker.ANY, mocker.ANY)


def test_main_cli_user_with_config(mocker, tmp_path):
    mocker.patch(
        "obs_automation.main.get_user_packages",
        return_value=[("P", "p1"), ("P", "p2"), ("P", "ignored_pkg")],
    )
    mock_process = mocker.patch("obs_automation.main.process_package")

    config_file = tmp_path / "config.yml"
    config_file.write_text("ignore:\n  - ignored_pkg\npackages:\n  - project: P\n    package: p1\n    anitya_id: 123")

    main(project=None, package=None, anitya_id=None, config=config_file, user="okurz")
    assert mock_process.call_count == 2
    mock_process.assert_any_call("P", "p1", "123", mocker.ANY, mocker.ANY)
    mock_process.assert_any_call("P", "p2", None, mocker.ANY, mocker.ANY)


def test_main_cli_user_error(mocker):
    mocker.patch("obs_automation.main.get_user_packages", return_value=[("P", "p")])
    mock_process = mocker.patch("obs_automation.main.process_package", side_effect=Exception("Failed"))

    main(project=None, package=None, anitya_id=None, config=None, user="okurz")
    mock_process.assert_called_once_with("P", "p", None, mocker.ANY, mocker.ANY)


@pytest.mark.parametrize(
    ("cli_project", "cli_package", "expected_targets"),
    [
        (None, "p", [("P", "p"), ("Q", "p")]),
        ("P", None, [("P", "p"), ("P", "other")]),
        ("P", "p", [("P", "p")]),
        ("Nope", None, []),
    ],
)
def test_main_cli_user_filters_by_project_and_package(mocker, cli_project, cli_package, expected_targets):
    mocker.patch(
        "obs_automation.main.get_user_packages",
        return_value=[("P", "p"), ("Q", "p"), ("P", "other")],
    )
    mock_process = mocker.patch("obs_automation.main.process_package")

    main(project=cli_project, package=cli_package, anitya_id=None, config=None, user="okurz")

    actual_targets = sorted((call.args[0], call.args[1]) for call in mock_process.call_args_list)
    assert actual_targets == sorted(expected_targets)


def test_main_cli_missing_args(mocker):
    mocker.patch("obs_automation.main.get_authenticated_user", return_value=None)
    with pytest.raises(SystemExit) as e:
        main(project=None, package=None, anitya_id=None, config=None, user=None)
    assert e.value.code == 1


def test_main_cli_default_user(mocker):
    mocker.patch("obs_automation.main.get_authenticated_user", return_value="okurz")
    mocker.patch("obs_automation.main.get_user_packages", return_value=[("P", "p")])
    mock_process = mocker.patch("obs_automation.main.process_package")

    main(project=None, package=None, anitya_id=None, config=None, user=None)
    mock_process.assert_called_once_with("P", "p", None, mocker.ANY, mocker.ANY)


def test_main_cli_exception(mocker):
    mocker.patch("obs_automation.main.get_user_packages", side_effect=Exception("Failed"))
    with pytest.raises(SystemExit) as e:
        main(project=None, package=None, anitya_id=None, config=None, user="okurz")
    assert e.value.code == 1


def test_process_package_retries_branch_on_transient_failure(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_sleep = mocker.patch("tenacity.nap.time.sleep")
    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"
    branch_calls = []

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            branch_calls.append(cmd)
            if len(branch_calls) == 1:
                raise _osc_error("Server returned an error: HTTP Error 503: Service Unavailable")
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"
    assert len(branch_calls) == 2
    assert mock_sleep.call_count == 1


def test_submit_and_service_commands_are_not_retried(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_readonly = mocker.patch("obs_automation.main.run_obs_readonly", side_effect=mock_run_cmd)
    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"
    mock_run_cmd.side_effect = lambda cmd, **_kwargs: mock_branch_res if cmd[0:2] == ["osc", "branch"] else _res("")

    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"

    wrapped = [cmd[0:2] for cmd in (c.args[0] for c in mock_readonly.call_args_list)]
    assert wrapped == [["osc", "branch"], ["osc", "update"]]
    submitted = [c.args[0][1] for c in mock_run_cmd.call_args_list if c.args[0][1] in {"ci", "sr"}]
    assert submitted == ["ci", "sr"]


def test_process_package_branch_fails_completely(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "some unknown error"
            raise err
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    with pytest.raises(RuntimeError, match="Failed to branch"):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_process_package_branch_dead_link(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "openSUSE:Factory/bitlbee-steam: package 'bitlbee-steam' does not exist"
            raise err
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    assert process_package(project="Project", package="bitlbee-steam", anitya_id="123") == "Skipped (dead link)"


def test_process_package_branch_already_exists(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "branch target package already exists: home:test:branches:Project/cf-cli"
            raise err
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"


def test_process_package_branch_parsing_fails_with_fallback(mocker, monkeypatch):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "unexpected stdout format"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    monkeypatch.setenv("OBS_USER", "fallbackuser")
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"


def test_process_package_branch_parsing_fails_no_fallback(mocker, monkeypatch):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "unexpected stdout format"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    monkeypatch.delenv("OBS_USER", raising=False)

    with pytest.raises(RuntimeError, match="Could not parse branched project name"):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_process_package_branch_parsing_fallback_via_whois(mocker, monkeypatch):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "unexpected stdout format"
    mock_whois_res = MagicMock()
    mock_whois_res.stdout = 'whoisuser: "Who Is" <who@example.com>\n'
    mock_whois_res.returncode = 0

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        if cmd == ["osc", "whois"]:
            return mock_whois_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    monkeypatch.delenv("OBS_USER", raising=False)
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"


def test_process_package_missing_service_file(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=False)

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Skipped (not a source package)"


def test_process_package_anitya_lookup_not_found(mocker):
    mocker.patch("obs_automation.main.get_obs_package_url", return_value="https://example.com")
    mocker.patch(
        "obs_automation.main.fetch_anitya_id_by_name_or_url",
        side_effect=ValueError("Could not find Anitya project"),
    )
    assert process_package(project="Project", package="unknown") == "Skipped (no Anitya project)"


def test_process_package_service_file_missing_revision(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<service name="tar_scm"></service>')

    with pytest.raises(
        RuntimeError, match=re.escape("Could not find current revision in _service or Version in .spec file.")
    ):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_get_user_packages_missing_attributes(mocker):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.stdout = """<collection><package name="cf-cli" /><package project="Cloud:Tools" /></collection>"""
    mock_run_cmd.return_value = mock_res

    assert get_user_packages("okurz") == []


def test_fetch_anitya_id_by_name_or_url_same_name(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": [{"id": 111, "name": "cf-cli"}]}
    mock_get.return_value = mock_resp

    assert fetch_anitya_id_by_name_or_url("cf-cli", "https://github.com/cf-cli/cf-cli.git") == "111"


def test_run_cmd_verbose(mocker):
    state["verbose"] = True
    mock_run = mocker.patch("subprocess.run")
    mock_res = MagicMock()
    mock_res.stdout = "hello stdout"
    mock_run.return_value = mock_res

    mock_print = mocker.patch("obs_automation.main.console.print")
    res = run_cmd(["echo", "hello"], capture_output=True)
    assert res == mock_res
    mock_print.assert_any_call("Running: echo hello")
    mock_print.assert_any_call("hello stdout")
    state["verbose"] = False


def test_run_cmd_error_not_verbose(mocker):
    state["verbose"] = False
    mock_run = mocker.patch("subprocess.run")
    err = subprocess.CalledProcessError(1, ["false"])
    err.stdout = "some stdout error"
    err.stderr = "some stderr error"
    mock_run.side_effect = err

    mock_print = mocker.patch("obs_automation.main.console.print")
    with pytest.raises(subprocess.CalledProcessError):
        run_cmd(["false"])

    mock_print.assert_any_call("some stdout error")
    mock_print.assert_any_call("some stderr error", style="red")


def test_fetch_anitya_id_by_name_or_url_exact_match(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": [{"id": 111, "name": "other-pkg"}, {"id": 222, "name": "cf-cli"}]}
    mock_get.return_value = mock_resp

    assert fetch_anitya_id_by_name_or_url("cf-cli", "https://github.com/cf-cli/cf-cli.git") == "222"


def test_fetch_anitya_id_by_name_or_url_one_item_fallback(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": [{"id": 333, "name": "something-else"}]}
    mock_get.return_value = mock_resp

    assert fetch_anitya_id_by_name_or_url("cf-cli") == "333"


def test_main_cli_config_with_ignore(mocker, tmp_path):
    mock_process = mocker.patch("obs_automation.main.process_package")

    config_file = tmp_path / "config.yml"
    config_file.write_text("ignore:\n  - p\npackages:\n  - project: P\n    package: p\n    anitya_id: 123")

    main(project=None, package=None, anitya_id=None, config=config_file, user=None, ignore=["other"])
    mock_process.assert_not_called()


def test_main_cli_verbose_and_ignore(mocker):
    mock_process = mocker.patch("obs_automation.main.process_package")
    main(project="P", package="p", anitya_id="123", config=None, user=None, verbose=True, ignore=["p"])
    assert state["verbose"] is True
    mock_process.assert_not_called()
    state["verbose"] = False


def test_fetch_anitya_id_by_name_or_url_multiple_names_loop_continue(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp_empty = MagicMock()
    mock_resp_empty.json.return_value = {"items": []}
    mock_resp_match = MagicMock()
    mock_resp_match.json.return_value = {"items": [{"id": 444, "name": "cli"}]}

    mock_get.side_effect = [mock_resp_empty, mock_resp_match]

    assert fetch_anitya_id_by_name_or_url("cf-cli", "https://github.com/cloudfoundry/cli.git") == "444"


def test_process_package_log_step_progress_and_verbose(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="1.2.3")
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Proj")

    mock_progress = MagicMock()
    process_package("Proj", "Pkg", "123", progress=mock_progress, task_id=45)
    mock_progress.update.assert_any_call(45, description="[cyan]Pkg:[/cyan] Latest Upstream version (Anitya): 1.2.3")

    state["verbose"] = True
    mock_console_print = mocker.patch("obs_automation.main.console.print")
    process_package("Proj", "Pkg", "123")
    mock_console_print.assert_any_call("Latest Upstream version (Anitya): 1.2.3")
    state["verbose"] = False


def test_main_cli_summary_colors(mocker, tmp_path):
    def mock_process_package(proj, pkg, a_id, progress=None, task_id=None):
        if pkg == "updated-pkg":
            return "Updated"
        if pkg == "skipped-pkg":
            return "Skipped (Snapshot)"
        if pkg == "failed-pkg":
            raise RuntimeError("Failure reason")
        return "Unknown"

    mocker.patch("obs_automation.main.process_package", side_effect=mock_process_package)
    mock_console_print = mocker.patch("obs_automation.main.console.print")

    config_file = tmp_path / "config.yml"
    config_file.write_text("""
ignore:
  - ignored-pkg
packages:
  - project: P
    package: updated-pkg
  - project: P
    package: skipped-pkg
  - project: P
    package: failed-pkg
""")
    main(project=None, package=None, anitya_id=None, config=config_file, user=None)

    assert any(call.args and isinstance(call.args[0], Table) for call in mock_console_print.call_args_list)


def test_run_cmd_error_verbose(mocker):
    state["verbose"] = True
    mock_run = mocker.patch("subprocess.run")
    err = subprocess.CalledProcessError(1, ["false"])
    err.stdout = "some stdout error"
    err.stderr = "some stderr error"
    mock_run.side_effect = err

    with pytest.raises(subprocess.CalledProcessError):
        run_cmd(["false"])
    state["verbose"] = False


def test_run_cmd_error_empty_outputs(mocker):
    state["verbose"] = False
    mock_run = mocker.patch("subprocess.run")
    err = subprocess.CalledProcessError(1, ["false"])
    err.stdout = None
    err.stderr = None
    mock_run.side_effect = err

    mock_print = mocker.patch("obs_automation.main.console.print")
    with pytest.raises(subprocess.CalledProcessError):
        run_cmd(["false"])
    mock_print.assert_not_called()


def test_fetch_anitya_id_by_name_or_url_multiple_no_match(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": [{"id": 111, "name": "diff1"}, {"id": 222, "name": "diff2"}]}
    mock_get.return_value = mock_resp

    with pytest.raises(ValueError, match="Could not find Anitya project"):
        fetch_anitya_id_by_name_or_url("cf-cli")


def test_main_cli_user_empty_packages(mocker):
    mocker.patch("obs_automation.main.get_user_packages", return_value=[])
    mock_console_print = mocker.patch("obs_automation.main.console.print")
    main(project=None, package=None, anitya_id=None, config=None, user="emptyuser")
    assert not any(call.args and isinstance(call.args[0], Table) for call in mock_console_print.call_args_list)


def test_process_package_snapshot_branch(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        return str(self).endswith("_service")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">master</param>')

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Skipped (Snapshot)"


def test_process_package_snapshot_sha(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        return str(self).endswith("_service")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch(
        "pathlib.Path.read_text", return_value='<param name="revision">7acac92a6543b593259b1689622d99d164537119</param>'
    )

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Skipped (Snapshot)"


def test_process_package_spec_bump(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        return str(self).endswith(".spec")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value="Version:        1.2.3")
    mock_write = mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"
    mock_write.assert_called_once_with("Version:        1.2.4")


def test_process_package_spec_bump_strip_v(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        return str(self).endswith(".spec")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value="Version:        1.2.3")
    mock_write = mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"
    mock_write.assert_called_once_with("Version:        1.2.4")


def test_process_package_service_bump_add_v(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        return str(self).endswith("_service")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mock_write = mocker.patch("pathlib.Path.write_text")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"
    mock_write.assert_called_once_with('<param name="revision">v1.2.4</param>')


def test_process_package_remove_obsolete(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        if cmd[0:2] == ["osc", "service"]:
            m.stdout = "###ASK /path/to/obsolete.tar.gz\n"
        else:
            m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        if str(self) == "/path/to/obsolete.tar.gz":
            return True
        return str(self).endswith(".spec")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value="Version:        1.2.3")
    mocker.patch("pathlib.Path.write_text")
    mock_unlink = mocker.patch("pathlib.Path.unlink")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"
    mock_unlink.assert_called_once()


def test_process_package_remove_obsolete_not_exist(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        m = MagicMock()
        if cmd[0:2] == ["osc", "service"]:
            m.stdout = "###ASK /path/to/obsolete.tar.gz\n"
        else:
            m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect

    def mock_exists(self):
        if str(self) == "/path/to/obsolete.tar.gz":
            return False
        return str(self).endswith(".spec")

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value="Version:        1.2.3")
    mocker.patch("pathlib.Path.write_text")
    mock_unlink = mocker.patch("pathlib.Path.unlink")

    assert process_package(project="Project", package="cf-cli", anitya_id="123") == "Updated"
    mock_unlink.assert_not_called()


def test_process_package_missing_runner_service(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        if cmd == ["osc", "service", "ra"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "*** Error: Package ('obs-service-obs_scm',) is required for this operation"
            err.stdout = ""
            raise err
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", lambda self: str(self).endswith(".spec"))
    mocker.patch("pathlib.Path.read_text", return_value="Version:        1.2.3")
    mocker.patch("pathlib.Path.write_text")

    assert (
        process_package(project="Project", package="cf-cli", anitya_id="123")
        == "Skipped (missing obs-service-obs_scm in runner)"
    )


def test_process_package_service_ra_unknown_error(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")

    def side_effect(cmd, **_kwargs):
        if cmd == ["osc", "service", "ra"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "general service error"
            err.stdout = ""
            raise err
        m = MagicMock()
        m.stdout = ""
        return m

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", lambda self: str(self).endswith(".spec"))
    mocker.patch("pathlib.Path.read_text", return_value="Version:        1.2.3")
    mocker.patch("pathlib.Path.write_text")

    with pytest.raises(subprocess.CalledProcessError):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_fetch_anitya_id_by_name_or_url_packages_mapping(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp_packages = mocker.MagicMock()
    mock_resp_packages.json.return_value = {"items": [{"distribution": "openSUSE", "project": "my-dist-pkg"}]}
    mock_resp_projects = mocker.MagicMock()
    mock_resp_projects.json.return_value = {"items": [{"id": 555, "name": "my-dist-pkg"}]}

    # First call to packages API, second to projects API
    mock_get.side_effect = [mock_resp_packages, mock_resp_projects]

    # Call with a package name
    assert fetch_anitya_id_by_name_or_url("python-my-dist-pkg") == "555"


def test_fetch_anitya_id_by_name_or_url_python_prefix(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp_packages = mocker.MagicMock()
    # No mapping found
    mock_resp_packages.json.return_value = {"items": []}

    mock_resp_proj_empty = mocker.MagicMock()
    mock_resp_proj_empty.json.return_value = {"items": []}

    mock_resp_proj_match = mocker.MagicMock()
    mock_resp_proj_match.json.return_value = {"items": [{"id": 666, "name": "zope.interface"}]}

    # 1. packages
    # 2. projects?name=python-zope.interface
    # 3. projects?name=zope.interface
    mock_get.side_effect = [mock_resp_packages, mock_resp_proj_empty, mock_resp_proj_match]

    assert fetch_anitya_id_by_name_or_url("python-zope.interface") == "666"


def test_fetch_anitya_id_by_name_or_url_python_prefix_already_in_list(mocker):
    mock_get = mocker.patch("httpx.get")
    mock_resp_packages = mocker.MagicMock()
    mock_resp_packages.json.return_value = {"items": []}

    mock_resp_proj_empty = mocker.MagicMock()
    mock_resp_proj_empty.json.return_value = {"items": []}

    mock_resp_proj_match = mocker.MagicMock()
    mock_resp_proj_match.json.return_value = {"items": [{"id": 777, "name": "zope.interface"}]}

    # 1. packages
    # 2. projects?name=python-zope.interface
    # 3. projects?name=zope.interface
    mock_get.side_effect = [mock_resp_packages, mock_resp_proj_empty, mock_resp_proj_match]

    assert (
        fetch_anitya_id_by_name_or_url("python-zope.interface", "https://github.com/zopefoundation/zope.interface.git")
        == "777"
    )


def test_process_package_dry_run(mocker):
    state["dry_run"] = True

    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mocker.patch("obs_automation.main.fetch_anitya_id_by_name_or_url", return_value="123")

    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mocker.patch("pathlib.Path.exists", side_effect=lambda: True)
    # Return an older version so it triggers an update
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.0.0</param>')
    mock_write_text = mocker.patch("pathlib.Path.write_text")

    res = process_package("Proj", "Pkg", "123")

    assert res == "Updated (dry-run)"
    # run_cmd shouldn't have been called for branch
    for call in mock_run_cmd.mock_calls:
        args = call.args[0]
        assert args[1] != "branch"
        assert args[1] != "ci"
        assert args[1] != "sr"
        assert args[1] != "service"

    mock_write_text.assert_not_called()

    state["dry_run"] = False


def test_process_package_dry_run_spec(mocker):
    state["dry_run"] = True

    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mocker.patch("obs_automation.main.fetch_anitya_id_by_name_or_url", return_value="123")

    mocker.patch("obs_automation.main.run_cmd")

    def mock_exists(self):
        return self.name.endswith(".spec") or self.name == "Pkg"

    mocker.patch("pathlib.Path.exists", mock_exists)
    mocker.patch("pathlib.Path.read_text", return_value="Version:  1.0.0")
    mock_write_text = mocker.patch("pathlib.Path.write_text")

    res = process_package("Proj", "Pkg", "123")

    assert res == "Updated (dry-run)"
    mock_write_text.assert_not_called()

    state["dry_run"] = False


def test_main_cli_dry_run(mocker):
    mocker.patch("obs_automation.main.process_package")

    main(project="Proj", package="pkg", anitya_id="123", config=None, user=None, dry_run=True)
    assert state.get("dry_run") is True
    state["dry_run"] = False


@pytest.mark.parametrize(
    ("xml_content", "service_dirs_exist", "installed_services", "expected_missing"),
    [
        (None, False, [], []),
        ("not-valid-xml", True, [], []),
        ("<services><service/></services>", True, [], []),
        ('<services><service name="tar_scm"/></services>', False, [], []),
        ('<services><service name="tar_scm"/></services>', True, ["tar_scm"], []),
        (
            '<services><service name="tar_scm"/><service name="obs_scm"/></services>',
            True,
            ["tar_scm"],
            ["obs_scm"],
        ),
    ],
)
def test_get_missing_services(tmp_path, xml_content, service_dirs_exist, installed_services, expected_missing):
    service_file = tmp_path / "_service"
    if xml_content is not None:
        service_file.write_text(xml_content, encoding="utf-8")

    bin_dir = tmp_path / "services_bin"
    if service_dirs_exist:
        bin_dir.mkdir()
        for svc in installed_services:
            (bin_dir / svc).write_text("dummy", encoding="utf-8")

    result = get_missing_services(service_file, service_dirs=(bin_dir,))
    assert result == expected_missing


def test_process_package_preflight_missing_service(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mocker.patch("obs_automation.main._get_branch_project", return_value="home:test:branches:Project")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mocker.patch("pathlib.Path.exists", lambda self: str(self).endswith("_service"))
    mocker.patch(
        "pathlib.Path.read_text",
        return_value='<services><service name="tar_scm"><param name="revision">v1.2.3</param></service></services>',
    )
    mocker.patch("pathlib.Path.write_text")
    mocker.patch("obs_automation.main.get_missing_services", return_value=["format_spec_file"])

    assert (
        process_package(project="Project", package="cf-cli", anitya_id="123")
        == "Skipped (missing obs-service-format_spec_file in runner)"
    )
    for call in mock_run_cmd.call_args_list:
        assert call.args[0][0:3] != ["osc", "service", "ra"]
