import subprocess
from unittest.mock import MagicMock

import pytest
import typer

from obs_automation.main import (
    _get_branch_project,
    fetch_anitya_id_by_name_or_url,
    fetch_latest_version,
    get_obs_package_url,
    get_user_packages,
    main,
    process_package,
    run_cmd,
)


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


def test_get_user_packages(mocker):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.stdout = """<collection><package name="cf-cli" project="Cloud:Tools" /></collection>"""
    mock_run_cmd.return_value = mock_res

    assert get_user_packages("okurz") == [("Cloud:Tools", "cf-cli")]


def test_process_package_already_up_to_date(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

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
        return MagicMock()

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
        return MagicMock()

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


def test_main_cli_user_error(mocker):
    mocker.patch("obs_automation.main.get_user_packages", return_value=[("P", "p")])
    mock_process = mocker.patch("obs_automation.main.process_package", side_effect=Exception("Failed"))

    main(project=None, package=None, anitya_id=None, config=None, user="okurz")
    mock_process.assert_called_once_with("P", "p", None, mocker.ANY, mocker.ANY)


def test_main_cli_missing_args():
    with pytest.raises(SystemExit) as e:
        main(project=None, package=None, anitya_id=None, config=None, user=None)
    assert e.value.code == 1


def test_main_cli_exception(mocker):
    mocker.patch("obs_automation.main.get_user_packages", side_effect=Exception("Failed"))
    with pytest.raises(SystemExit) as e:
        main(project=None, package=None, anitya_id=None, config=None, user="okurz")
    assert e.value.code == 1


def test_process_package_branch_fails_completely(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "some unknown error"
            raise err
        return MagicMock()

    mock_run_cmd.side_effect = side_effect

    with pytest.raises(RuntimeError, match="Failed to branch"):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_process_package_branch_already_exists(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "branch target package already exists: home:test:branches:Project/cf-cli"
            raise err
        return MagicMock()

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
        return MagicMock()

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
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    monkeypatch.delenv("OBS_USER", raising=False)

    with pytest.raises(RuntimeError, match="Could not parse branched project name"):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_process_package_missing_service_file(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=False)

    with pytest.raises(RuntimeError, match="_service file not found!"):
        process_package(project="Project", package="cf-cli", anitya_id="123")


def test_process_package_service_file_missing_revision(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<service name="tar_scm"></service>')

    with pytest.raises(RuntimeError, match="Could not find current revision in _service file."):
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
