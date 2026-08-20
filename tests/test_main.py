import subprocess
from unittest.mock import MagicMock

import pytest
from rich.table import Table

from obs_automation.main import (
    _get_branch_project,
    fetch_anitya_id_by_name_or_url,
    fetch_latest_version,
    get_obs_package_url,
    get_user_packages,
    main,
    process_package,
    run_cmd,
    state,
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
        m = MagicMock()
        m.stdout = ""
        return m

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

    with pytest.raises(RuntimeError, match="Could not find current revision in _service or Version in .spec file."):
        process_package(project="Project", package="cf-cli", anitya_id="123")


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

    with pytest.raises(RuntimeError, match="Could not find current revision in _service or Version in .spec file."):
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


def test_get_obs_package_url_non_zero_exit(mocker):
    mock_run_cmd = mocker.patch("obs_automation.main.run_cmd")
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_run_cmd.return_value = mock_res
    assert get_obs_package_url("Project", "Package") is None


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
