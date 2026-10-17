"""OBS automation tooling for headless package bumping."""

import contextlib
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
import yaml
from defusedxml import ElementTree  # type: ignore[import-untyped]
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from tenacity import retry, stop_after_attempt, wait_fixed

app = typer.Typer()
console = Console()
state = {"verbose": False, "dry_run": False}


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2), reraise=True)
def fetch_latest_version(anitya_id: str) -> str:
    """Fetch the latest version of a project from Anitya."""
    url = f"https://release-monitoring.org/api/project/{anitya_id}"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    version = resp.json().get("version")
    if not version:
        raise ValueError("Anitya API returned empty version")
    return str(version)


def run_cmd(
    cmd: list[str], check: bool = True, capture_output: bool = False, **kwargs: Any
) -> subprocess.CompletedProcess:
    """Run a subprocess command safely."""
    if state["verbose"]:
        console.print(f"Running: {' '.join(cmd)}")

    # Hide output by default unless explicitly capturing for parsing or verbose is enabled
    if not state["verbose"] or capture_output:
        kwargs["capture_output"] = True

    try:
        res = subprocess.run(cmd, check=check, text=True, **kwargs)
        if state["verbose"] and capture_output and res.stdout:
            console.print(res.stdout)
    except subprocess.CalledProcessError as e:
        # If it failed and we were hiding output, print it now for context
        if not state["verbose"]:
            if e.stdout:
                console.print(e.stdout)
            if e.stderr:
                console.print(e.stderr, style="red")
        raise
    else:
        return res


def _get_branch_project(stdout: str) -> str | None:
    for line in stdout.splitlines():
        match = re.search(r"([^\s:]+:[^\s]+)/[^\s]+", line)
        if match:
            return match.group(1)
    return None


def get_obs_package_url(project: str, package: str) -> str | None:
    """Extract upstream URL from OBS meta configuration."""
    res = run_cmd(["osc", "api", f"/source/{project}/{package}/_meta"], capture_output=True, check=False)
    if res.returncode != 0:
        return None
    root = ElementTree.fromstring(res.stdout)
    url_elem = root.find("url")
    if url_elem is None or not url_elem.text:
        return None
    return url_elem.text


def fetch_anitya_id_by_name_or_url(package_name: str, url: str | None = None) -> str:
    """Lookup Anitya ID based on upstream URL and package name."""
    names_to_try = [package_name]
    base_url = None

    # Try mapping using Anitya's packages API first
    with contextlib.suppress(Exception):
        pkg_resp = httpx.get(f"https://release-monitoring.org/api/v2/packages/?name={package_name}", timeout=10.0)
        pkg_resp.raise_for_status()
        for pkg_item in pkg_resp.json().get("items", []):
            proj_name = pkg_item.get("project")
            if pkg_item.get("distribution") == "openSUSE" and proj_name and proj_name not in names_to_try:
                # Prioritize this mapped name by inserting it at the beginning
                names_to_try.insert(0, proj_name)

    if package_name.startswith("python-"):
        stripped = package_name.removeprefix("python-")
        if stripped not in names_to_try:
            names_to_try.append(stripped)

    if url:
        base_url = url.removesuffix(".git").rstrip("/")
        repo_name = base_url.split("/")[-1]
        if repo_name != package_name:
            names_to_try.append(repo_name)

    for name in names_to_try:
        resp = httpx.get(f"https://release-monitoring.org/api/v2/projects/?name={name}", timeout=10.0)
        resp.raise_for_status()
        items = resp.json().get("items", [])

        if not items:
            continue

        if base_url:
            for item in items:
                for prop in ["homepage", "ecosystem", "version_url"]:
                    prop_val = item.get(prop)
                    if prop_val and base_url in prop_val:
                        return str(item["id"])

        # Fallback if no URL matched, or if no URL was provided:
        # Check if there is exactly one exact match by name
        exact_matches = [i for i in items if i.get("name") == name]
        if len(exact_matches) == 1:
            return str(exact_matches[0]["id"])
        if len(items) == 1:
            return str(items[0]["id"])

    raise ValueError(f"Could not find Anitya project for {package_name} (URL: {url})")


def get_user_packages(user: str) -> list[tuple[str, str]]:
    """Retrieve all packages maintained by a specific OBS user."""
    query_user = user.strip()
    res = run_cmd(["osc", "api", f"/search/package?match=person/@userid='{query_user}'"], capture_output=True)
    root = ElementTree.fromstring(res.stdout)
    packages = []
    for pkg in root.findall("package"):
        project = pkg.get("project")
        name = pkg.get("name")
        if project and name:
            packages.append((project, name))
    return packages


def get_authenticated_user() -> str | None:
    """Determine the OBS account that osc authenticates as, if determinable.

    Prefers the OBS_USER environment variable (used in CI) and falls back to
    querying the server. This is deliberately independent of the account passed
    to --user, which only selects whose maintained packages to enumerate.
    """
    env_user = os.environ.get("OBS_USER")
    if env_user and env_user.strip():
        return env_user.strip()
    res = run_cmd(["osc", "whois"], capture_output=True, check=False)
    if res.returncode != 0 or ":" not in res.stdout:
        return None
    return res.stdout.split(":", 1)[0].strip() or None


@dataclass
class BumpTarget:
    """Bump target metadata and resolution outcome."""

    target_file: Path
    skip_reason: str | None = None
    current_tag: str = ""
    latest_tag: str = ""
    is_spec_bump: bool = False
    content: str = ""


def _format_target_tag(current_tag: str, latest_version: str) -> str:
    """Format the latest version to match the v-prefix style of the current tag."""
    if current_tag.startswith("v") and not latest_version.startswith("v"):
        return f"v{latest_version}"
    if not current_tag.startswith("v") and latest_version.startswith("v"):
        return latest_version.lstrip("v")
    return latest_version


def _inspect_bump_target(workdir: Path, package: str, latest_version: str, log_step: Any) -> BumpTarget:
    """Inspect local checked out package files and determine bump suitability."""
    service_file = workdir / "_service"
    spec_file = workdir / f"{package}.spec"

    if not service_file.exists() and not spec_file.exists():
        log_step("No _service or .spec file found. Skipping.")
        return BumpTarget(target_file=workdir, skip_reason="Skipped (not a source package)")

    current_tag = None
    is_spec_bump = False
    content = ""
    target_file = service_file

    if service_file.exists():
        content = service_file.read_text()
        current_tag_match = re.search(r'<param name="revision">([^<]+)</param>', content)
        if current_tag_match:
            current_tag = current_tag_match.group(1)

    if not current_tag and spec_file.exists():
        content = spec_file.read_text()
        current_tag_match = re.search(r"^Version:\s+([^\s]+)", content, flags=re.MULTILINE)
        if current_tag_match:
            current_tag = current_tag_match.group(1)
            is_spec_bump = True
            target_file = spec_file

    if not current_tag:
        raise RuntimeError("Could not find current revision in _service or Version in .spec file.")

    log_step(f"Current OBS version: {current_tag}")

    if current_tag in {"master", "main", "develop"} or re.fullmatch(r"[0-9a-f]{40}", current_tag):
        log_step("Package tracks a snapshot/branch. Skipping to prevent downgrade.")
        return BumpTarget(
            target_file=target_file,
            skip_reason="Skipped (Snapshot)",
            current_tag=current_tag,
        )

    latest_tag = _format_target_tag(current_tag, latest_version)

    if current_tag == latest_tag:
        log_step("Package is already up to date. Skipping.")
        return BumpTarget(
            target_file=target_file,
            skip_reason="Skipped",
            current_tag=current_tag,
            latest_tag=latest_tag,
        )

    return BumpTarget(
        target_file=target_file,
        current_tag=current_tag,
        latest_tag=latest_tag,
        is_spec_bump=is_spec_bump,
        content=content,
    )


OBS_SERVICE_DIRS = (Path("/usr/lib/obs/service"), Path("/usr/libexec/obs/service"))


def get_missing_services(
    service_file: Path,
    service_dirs: tuple[Path, ...] = OBS_SERVICE_DIRS,
) -> list[str]:
    """Check if all services requested in _service are installed on the system."""
    if not service_file.exists():
        return []
    try:
        root = ElementTree.fromstring(service_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    missing = []
    for service in root.findall("service"):
        name = service.get("name")
        if not name:
            continue
        if any(d.exists() for d in service_dirs) and not any((d / name).exists() for d in service_dirs):
            missing.append(name)
    return missing


def _run_services_and_submit(
    workdir: Path,
    branch_project: str,
    package: str,
    latest_tag: str,
    log_step: Any,
) -> str:
    """Run local source services, commit changes, and create submit request."""
    service_file = workdir / "_service"
    missing_services = get_missing_services(service_file)
    if missing_services:
        missing_names = ", ".join(f"obs-service-{s}" for s in missing_services)
        log_step(f"Runner is missing required OBS service(s): {missing_names}. Skipping.")
        return f"Skipped (missing {missing_names} in runner)"

    log_step("Downloading updated source files...")
    run_cmd(["osc", "service", "run", "download_files"], cwd=str(workdir))
    run_cmd(["osc", "addremove"], cwd=str(workdir))

    log_step("Running OBS services locally to generate tarballs and spec updates...")
    try:
        ra_res = run_cmd(["osc", "service", "ra"], cwd=str(workdir), capture_output=True)
    except subprocess.CalledProcessError as e:
        output = (e.stderr or "") + (e.stdout or "")
        match = re.search(r"Package\s+[^a-zA-Z0-9_-]*([a-zA-Z0-9_-]+).*?is required", output)
        if match:
            missing_pkg = match.group(1)
            log_step(f"Runner is missing required OBS service package: {missing_pkg}. Skipping.")
            return f"Skipped (missing {missing_pkg} in runner)"
        raise

    for match in re.finditer(r"^###ASK\s+(.+)$", ra_res.stdout, flags=re.MULTILINE):
        obsolete_file = Path(match.group(1))
        if obsolete_file.exists():
            log_step(f"Removing obsolete file: {obsolete_file.name}")
            obsolete_file.unlink()

    log_step("Cleaning up old tracked files and adding new ones...")
    run_cmd(["osc", "addremove"], cwd=str(workdir))

    log_step(f"Committing to {branch_project}...")
    run_cmd(["osc", "ci", "-m", f"Update {package} to {latest_tag}"], cwd=str(workdir))

    log_step("Creating Submit Request...")
    run_cmd(
        [
            "osc",
            "sr",
            "-m",
            f"Automated update to {latest_tag} based on Anitya release monitoring",
        ],
        cwd=str(workdir),
    )
    log_step(f"Done processing {package}!")
    return "Updated"


def process_package(
    project: str,
    package: str,
    anitya_id: str | None = None,
    progress: Progress | None = None,
    task_id: Any | None = None,
) -> str:
    """Run the bump logic for a single package. Returns 'Updated', 'Skipped', or raises an Exception."""

    def log_step(msg: str) -> None:
        if progress and task_id is not None:
            progress.update(task_id, description=f"[cyan]{package}:[/cyan] {msg}")
        elif state["verbose"]:
            console.print(msg)

    if not anitya_id:
        log_step(f"Looking up Anitya ID for {project}/{package}...")
        url = get_obs_package_url(project, package)
        try:
            anitya_id = fetch_anitya_id_by_name_or_url(package, url)
        except ValueError:
            log_step("Could not find Anitya project. Skipping.")
            return "Skipped (no Anitya project)"
        log_step(f"Found Anitya ID: {anitya_id}")

    latest_version = fetch_latest_version(anitya_id)
    log_step(f"Latest Upstream version (Anitya): {latest_version}")

    log_step(f"Branching from {project} in OBS...")
    branch_project = None
    if state.get("dry_run"):
        log_step("[dry-run] Skipping branch creation, using original project for local checkout.")
        branch_project = project
    else:
        try:
            branch_res = run_cmd(["osc", "branch", project, package], capture_output=True)
            branch_project = _get_branch_project(branch_res.stdout)
        except subprocess.CalledProcessError as e:
            if "does not exist" in e.stderr:
                log_step("Target package does not exist. Skipping.")
                return "Skipped (dead link)"
            for line in e.stderr.splitlines():
                if "already exists:" in line:
                    branch_project = _get_branch_project(line)
                    break
            if not branch_project:
                raise RuntimeError(f"Failed to branch: {e.stderr}") from None

        if not branch_project:
            user = get_authenticated_user()
            if not user:
                raise RuntimeError("Could not parse branched project name and no authenticated OBS user found.")
            branch_project = f"home:{user}:branches:{project}"

    log_step(f"Working with branched project: {branch_project}")

    log_step("Checking out OBS package...")
    workdir = Path(branch_project) / package

    if workdir.exists():
        run_cmd(["osc", "update"], cwd=str(workdir))
    else:
        # Check out into the branch_project directory
        run_cmd(["osc", "checkout", branch_project, package])

    target = _inspect_bump_target(workdir, package, latest_version, log_step)
    if target.skip_reason:
        return target.skip_reason

    if target.is_spec_bump:
        log_step(f"Updating .spec file Version to {target.latest_tag}...")
        if not state.get("dry_run"):
            new_content = re.sub(
                r"^(Version:\s+)[^\s]+",
                rf"\g<1>{target.latest_tag}",
                target.content,
                flags=re.MULTILINE,
            )
            target.target_file.write_text(new_content)
        else:
            log_step(f"[dry-run] Would update .spec file Version to {target.latest_tag}")
    else:
        log_step(f"Updating _service file to {target.latest_tag}...")
        if not state.get("dry_run"):
            new_content = re.sub(
                r'<param name="revision">[^<]+</param>',
                f'<param name="revision">{target.latest_tag}</param>',
                target.content,
            )
            target.target_file.write_text(new_content)
        else:
            log_step(f"[dry-run] Would update _service file to {target.latest_tag}")

    if state.get("dry_run"):
        log_step("[dry-run] Skipping running services, committing, and submitting.")
        return "Updated (dry-run)"

    return _run_services_and_submit(workdir, branch_project, package, target.latest_tag, log_step)


def _resolve_targets(
    config: Path | None,
    user: str | None,
    project: str | None,
    package: str | None,
    anitya_id: str | None,
    ignore: list[str],
) -> tuple[list[tuple[str, str, str | None]], set[str]]:
    """Resolve packages to process and the ignore set from the CLI arguments/config."""
    ignore_set = set(ignore)
    config_packages: list[tuple[str, str, str | None]] = []
    config_anitya_map: dict[tuple[str, str], str] = {}
    if config:
        with config.open() as f:
            data = yaml.safe_load(f) or {}
        ignore_set.update(data.get("ignore", []))
        for item in data.get("packages", []):
            pkg_proj = item["project"]
            pkg_name = item["package"]
            a_id = str(item["anitya_id"]) if item.get("anitya_id") else None
            config_packages.append((pkg_proj, pkg_name, a_id))
            if a_id:
                config_anitya_map[pkg_proj, pkg_name] = a_id

    if user:
        packages = [
            (proj, pkg, config_anitya_map.get((proj, pkg)))
            for proj, pkg in get_user_packages(user)
            if (project is None or proj == project) and (package is None or pkg == package)
        ]
    elif config:
        packages = config_packages
    elif project and package:
        packages = [(project, package, anitya_id)]
    else:
        print("Error: Must provide either --config, --user, or both --project and --package.")
        raise typer.Exit(code=1)
    return packages, ignore_set


def _process_packages(
    packages_to_process: list[tuple[str, str, str | None]],
    ignore_set: set[str],
) -> list[tuple[str, str, str, str]]:
    """Process all packages concurrently and collect their results."""
    results: list[tuple[str, str, str, str]] = []
    with (
        Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress,
        ThreadPoolExecutor(max_workers=4) as executor,
    ):
        futures = {}
        for proj, pkg, a_id in packages_to_process:
            if pkg in ignore_set:
                results.append((proj, pkg, "Ignored", "Package is in ignore list"))
                continue

            task_id = progress.add_task(f"[cyan]{pkg}:[/cyan] Starting...", total=None)
            future = executor.submit(process_package, proj, pkg, a_id, progress, task_id)
            futures[future] = (proj, pkg, task_id)

        for future in as_completed(futures):
            proj, pkg, task_id = futures[future]
            try:
                status = future.result()
                results.append((proj, pkg, status, ""))
            except Exception as e:
                msg = str(e) or repr(e)
                results.append((proj, pkg, "Failed", msg))
                progress.console.print(f"[red]Failed processing {proj}/{pkg}: {msg}[/red]")
            finally:
                progress.remove_task(task_id)
    return results


def _print_summary(results: list[tuple[str, str, str, str]]) -> None:
    print("\n")
    table = Table("Project", "Package", "Status", "Details", title="Auto-Bump Summary")
    for proj, pkg, status, details in results:
        if status == "Updated":
            color = "green"
        elif status.startswith("Skipped"):
            color = "yellow"
        elif status == "Ignored":
            color = "blue"
        else:
            color = "red"
        table.add_row(proj, pkg, f"[{color}]{status}[/{color}]", details)
    console.print(table)


@app.command()
def main(
    project: str | None = typer.Option(None, help="Main OBS project (e.g. Cloud:Tools)"),
    package: str | None = typer.Option(None, help="OBS package name (e.g. cf-cli)"),
    anitya_id: str | None = typer.Option(None, help="Anitya project ID"),
    config: Path | None = typer.Option(None, help="Path to YAML config file"),
    user: str | None = typer.Option(
        None, help="OBS username whose maintained packages to process (independent of the osc account used to submit)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose output"),
    dry_run: bool = typer.Option(
        False, "--dry-run", "--dry", help="Perform a dry run without making any changes in OBS"
    ),
    ignore: list[str] = typer.Option([], "--ignore", "-i", help="Packages to ignore (can be used multiple times)"),
) -> None:
    """Run the OBS auto-bump process."""
    is_verbose = verbose if not isinstance(verbose, typer.models.OptionInfo) else False
    if is_verbose:
        state["verbose"] = True
    is_dry_run = dry_run if not isinstance(dry_run, typer.models.OptionInfo) else False
    if is_dry_run:
        state["dry_run"] = True

    actual_ignore = ignore if not isinstance(ignore, typer.models.OptionInfo) else []

    try:
        packages_to_process, ignore_set = _resolve_targets(config, user, project, package, anitya_id, actual_ignore)
        results = _process_packages(packages_to_process, ignore_set)
        if results:
            _print_summary(results)
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
