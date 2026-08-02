"""OBS automation tooling for headless package bumping."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml
from defusedxml import ElementTree  # type: ignore[import-untyped]
from tenacity import retry, stop_after_attempt, wait_fixed

app = typer.Typer()


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2), reraise=True)
def fetch_latest_version(anitya_id: str) -> str:
    """Fetch the latest version of a project from Anitya."""
    print(f"Fetching latest version for Anitya ID {anitya_id}...")
    url = f"https://release-monitoring.org/api/project/{anitya_id}"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    version = resp.json().get("version")
    if not version:
        raise ValueError("Anitya API returned empty version")
    return str(version)


def run_cmd(cmd: list[str], check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a subprocess command safely."""
    print(f"Running: {" ".join(cmd)}")
    return subprocess.run(cmd, check=check, text=True, **kwargs)


def _get_branch_project(stdout: str) -> str | None:
    for line in stdout.splitlines():
        match = re.search(r"([^\s:]+:[^\s]+)/[^\s]+", line)
        if match:
            return match.group(1)
    return None


def get_obs_package_url(project: str, package: str) -> str:
    """Extract upstream URL from OBS meta configuration."""
    res = run_cmd(["osc", "api", f"/source/{project}/{package}/_meta"], capture_output=True)
    root = ElementTree.fromstring(res.stdout)
    url_elem = root.find("url")
    if url_elem is None or not url_elem.text:
        raise ValueError(f"No <url> element found in OBS meta for {project}/{package}")
    return url_elem.text


def fetch_anitya_id_by_url(url: str) -> str:
    """Lookup Anitya ID based on upstream URL."""
    base_url = url.removesuffix(".git")
    for param in ["url", "homepage"]:
        resp = httpx.get(f"https://release-monitoring.org/api/v2/projects/?{param}={base_url}", timeout=10.0)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            return str(items[0]["id"])
    raise ValueError(f"Could not find Anitya project for URL: {url}")


def get_user_packages(user: str) -> list[tuple[str, str]]:
    """Retrieve all packages maintained by a specific OBS user."""
    res = run_cmd(["osc", "api", f"/search/package?match=person/@userid='{user}'"], capture_output=True)
    root = ElementTree.fromstring(res.stdout)
    packages = []
    for pkg in root.findall("package"):
        project = pkg.get("project")
        name = pkg.get("name")
        if project and name:
            packages.append((project, name))
    return packages


def process_package(project: str, package: str, anitya_id: str | None = None) -> None:
    """Run the bump logic for a single package."""
    if not anitya_id:
        print(f"Looking up Anitya ID for {project}/{package}...")
        url = get_obs_package_url(project, package)
        anitya_id = fetch_anitya_id_by_url(url)
        print(f"Found Anitya ID: {anitya_id}")

    latest_tag = fetch_latest_version(anitya_id)
    if not latest_tag.startswith("v"):
        latest_tag = f"v{latest_tag}"

    print(f"Latest Upstream version (Anitya): {latest_tag}")

    print(f"Branching the package {package} from {project} in OBS...")
    branch_project = None
    try:
        branch_res = run_cmd(["osc", "branch", project, package], capture_output=True)
        branch_project = _get_branch_project(branch_res.stdout)
    except subprocess.CalledProcessError as e:
        for line in e.stderr.splitlines():
            if "already exists:" in line:
                branch_project = _get_branch_project(line)
                break
        if not branch_project:
            print(f"Failed to branch: {e.stderr}")
            raise typer.Exit(code=1) from None

    if not branch_project:
        user = os.environ.get("OBS_USER")
        if not user:
            print("Could not parse branched project name and OBS_USER not set.")
            raise typer.Exit(code=1) from None
        branch_project = f"home:{user}:branches:{project}"

    print(f"Working with branched project: {branch_project}")

    print("Checking out OBS package...")
    run_cmd(["osc", "checkout", branch_project, package])

    # Save current working directory so we can return if looping over multiple packages
    original_cwd = Path.cwd()
    workdir = Path(f"{branch_project}/{package}")
    os.chdir(workdir)

    try:
        service_file = Path("_service")
        if not service_file.exists():
            print("_service file not found!")
            raise typer.Exit(code=1)

        content = service_file.read_text()
        current_tag_match = re.search(r'<param name="revision">([^<]+)</param>', content)
        if not current_tag_match:
            print("Could not find current revision in _service file.")
            raise typer.Exit(code=1)

        current_tag = current_tag_match.group(1)
        print(f"Current OBS version: {current_tag}")

        if current_tag == latest_tag:
            print("Package is already up to date. Skipping.")
            return

        print(f"Updating _service file to {latest_tag}...")
        new_content = re.sub(
            r'<param name="revision">[^<]+</param>',
            f'<param name="revision">{latest_tag}</param>',
            content,
        )
        service_file.write_text(new_content)

        print("Running OBS services locally to generate tarballs and spec updates...")
        run_cmd(["osc", "service", "ra"])

        print("Cleaning up old tracked files and adding new ones...")
        run_cmd(["osc", "addremove"])

        print(f"Committing to {branch_project}...")
        run_cmd(["osc", "ci", "-m", f"Update {package} to {latest_tag}"])

        print("Creating Submit Request...")
        run_cmd([
            "osc",
            "sr",
            "-m",
            f"Automated update to {latest_tag} based on Anitya release monitoring",
        ])
        print(f"Done processing {package}!")
    finally:
        os.chdir(original_cwd)


@app.command()
def main(
    project: str | None = typer.Option(None, help="Main OBS project (e.g. Cloud:Tools)"),
    package: str | None = typer.Option(None, help="OBS package name (e.g. cf-cli)"),
    anitya_id: str | None = typer.Option(None, help="Anitya project ID"),
    config: Path | None = typer.Option(None, help="Path to YAML config file"),
    user: str | None = typer.Option(None, help="OBS username to process all maintained packages"),
) -> None:
    """Run the OBS auto-bump process."""
    try:
        if config:
            with config.open() as f:
                data = yaml.safe_load(f)
                for item in data.get("packages", []):
                    try:
                        process_package(
                            item["project"], item["package"], str(item["anitya_id"]) if item.get("anitya_id") else None
                        )
                    except Exception as e:
                        print(f"Failed processing {item["project"]}/{item["package"]}: {e}")
            return

        if user:
            for proj, pkg in get_user_packages(user):
                try:
                    process_package(proj, pkg)
                except Exception as e:
                    print(f"Failed processing {proj}/{pkg}: {e}")
            return

        if project and package:
            process_package(project, package, anitya_id)
            return

        print("Error: Must provide either --config, --user, or both --project and --package.")
        raise typer.Exit(code=1)

    except typer.Exit as e:
        sys.exit(e.exit_code)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
