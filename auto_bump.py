#!/usr/bin/env python3

import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import typer
from tenacity import retry, stop_after_attempt, wait_fixed

app = typer.Typer()


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def fetch_latest_version(anitya_id: str) -> str:
    print(f"Fetching latest version for Anitya ID {anitya_id}...")
    url = f"https://release-monitoring.org/api/project/{anitya_id}"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    version = resp.json().get("version")
    if not version:
        raise ValueError("Anitya API returned empty version")
    return version


def run_cmd(cmd: list[str], check: bool = True, **kwargs):
    print(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True, **kwargs)


@app.command()
def main(
    project: str = typer.Option(..., help="Main OBS project (e.g. Cloud:Tools)"),
    package: str = typer.Option(..., help="OBS package name (e.g. cf-cli)"),
    anitya_id: str = typer.Option(..., help="Anitya project ID"),
):
    try:
        latest_tag = fetch_latest_version(anitya_id)
        if not latest_tag.startswith("v"):
            latest_tag = f"v{latest_tag}"

        print(f"Latest Upstream version (Anitya): {latest_tag}")

        # Branch the project
        print(f"Branching the package {package} from {project} in OBS...")
        branch_project = None
        try:
            branch_res = run_cmd(
                ["osc", "branch", project, package], capture_output=True
            )
            for line in branch_res.stdout.splitlines():
                match = re.search(rf"([^\s:]+:[^\s]+)/{package}", line)
                if match:
                    branch_project = match.group(1)
                    break
        except subprocess.CalledProcessError as e:
            # Check if it failed because it already exists
            for line in e.stderr.splitlines():
                if "already exists:" in line:
                    match = re.search(rf"([^\s:]+:[^\s]+)/{package}", line)
                    if match:
                        branch_project = match.group(1)
                        break
            if not branch_project:
                print(f"Failed to branch: {e.stderr}")
                sys.exit(1)

        if not branch_project:
            # Fallback if parsing fails (assume standard home:USER:branches:...)
            user = os.environ.get("OBS_USER")
            if not user:
                print("Could not parse branched project name and OBS_USER not set.")
                sys.exit(1)
            branch_project = f"home:{user}:branches:{project}"

        print(f"Working with branched project: {branch_project}")

        print("Checking out OBS package...")
        run_cmd(["osc", "checkout", branch_project, package])

        workdir = Path(f"{branch_project}/{package}")
        os.chdir(workdir)

        service_file = Path("_service")
        if not service_file.exists():
            print("_service file not found!")
            sys.exit(1)

        content = service_file.read_text()
        current_tag_match = re.search(
            r'<param name="revision">([^<]+)</param>', content
        )
        if not current_tag_match:
            print("Could not find current revision in _service file.")
            sys.exit(1)

        current_tag = current_tag_match.group(1)
        print(f"Current OBS version: {current_tag}")

        if current_tag == latest_tag:
            print("Package is already up to date. Exiting.")
            sys.exit(0)

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
        run_cmd(
            [
                "osc",
                "sr",
                "-m",
                f"Automated update to {latest_tag} based on Anitya release monitoring",
            ]
        )
        print("Done!")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    app()
