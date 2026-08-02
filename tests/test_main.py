import subprocess
from unittest.mock import MagicMock

import pytest

from obs_automation.main import _get_branch_project, fetch_latest_version, main, run_cmd


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
    mock_run.assert_called_once_with(["ls", "-l"], check=False, text=True)


def test_get_branch_project():
    stdout = (
        "A working copy of the branched package can be checked out with:\n\n"
        "osc co home:okurz:branches:Cloud:Tools/cf-cli\n"
    )
    assert _get_branch_project(stdout) == "home:okurz:branches:Cloud:Tools"

    assert _get_branch_project("no match here") is None


def test_main_already_up_to_date(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.3")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("os.chdir")

    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')

    with pytest.raises(SystemExit) as e:
        main(project="Project", package="cf-cli", anitya_id="123")
    assert e.value.code == 0


def test_main_update_flow(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("os.chdir")

    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mock_write_text = mocker.patch("pathlib.Path.write_text")

    main(project="Project", package="cf-cli", anitya_id="123")

    mock_write_text.assert_called_once_with('<param name="revision">v1.2.4</param>')
    assert any(c.args[0] == ["osc", "ci", "-m", "Update cf-cli to v1.2.4"] for c in mock_run_cmd.call_args_list)


def test_main_branch_already_exists(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "branch target package already exists: home:test:branches:Project/cf-cli"
            raise err
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("os.chdir")
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    main(project="Project", package="cf-cli", anitya_id="123")


def test_main_branch_fails_completely(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            err = subprocess.CalledProcessError(1, cmd)
            err.stderr = "some unknown error"
            raise err
        return MagicMock()

    mock_run_cmd.side_effect = side_effect

    with pytest.raises(SystemExit) as e:
        main(project="Project", package="cf-cli", anitya_id="123")
    assert e.value.code == 1


def test_main_branch_parsing_fails_with_fallback(mocker, monkeypatch):
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
    mocker.patch("os.chdir")
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<param name="revision">v1.2.3</param>')
    mocker.patch("pathlib.Path.write_text")

    main(project="Project", package="cf-cli", anitya_id="123")


def test_main_branch_parsing_fails_no_fallback(mocker, monkeypatch):
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

    with pytest.raises(SystemExit) as e:
        main(project="Project", package="cf-cli", anitya_id="123")
    assert e.value.code == 1


def test_main_missing_service_file(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("os.chdir")
    mocker.patch("pathlib.Path.exists", return_value=False)

    with pytest.raises(SystemExit) as e:
        main(project="Project", package="cf-cli", anitya_id="123")
    assert e.value.code == 1


def test_main_service_file_missing_revision(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", return_value="v1.2.4")
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")

    mock_branch_res = MagicMock()
    mock_branch_res.stdout = "home:test:branches:Project/cf-cli"

    def side_effect(cmd, **_kwargs):
        if cmd[0:2] == ["osc", "branch"]:
            return mock_branch_res
        return MagicMock()

    mock_run_cmd.side_effect = side_effect
    mocker.patch("os.chdir")
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.read_text", return_value='<service name="tar_scm"></service>')

    with pytest.raises(SystemExit) as e:
        main(project="Project", package="cf-cli", anitya_id="123")
    assert e.value.code == 1


def test_main_general_exception(mocker):
    mocker.patch("obs_automation.main.fetch_latest_version", side_effect=RuntimeError("Unexpected!"))

    with pytest.raises(SystemExit) as e:
        main(project="Project", package="cf-cli", anitya_id="123")
    assert e.value.code == 1
