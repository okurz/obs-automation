# obs-automation

Headless package bumping and automation for the openSUSE Build Service (OBS).
It monitors [Anitya](https://release-monitoring.org/) for upstream releases and automatically creates branched Submit Requests via a GitHub Actions pipeline.

## Features

- **Decoupled from GitHub Rate Limits:** Leverages the open-source Anitya ecosystem.
- **Resilient:** Uses HTTPx with Tenacity for network retries; transient OBS failures are retried on read-only `osc` calls (4 attempts, exponential backoff), and the CI job runs with `--verbose` so each `osc` command is logged.
- **Stateless/Headless:** Runs completely isolated in CI without requiring local OS state.
- **Safe:** Branches the target project, ensuring human maintainers review the changes via Submit Requests.

## Usage

A convenient wrapper script is provided in the top directory to execute the tool locally. You must have [uv](https://github.com/astral-sh/uv) installed.

```bash
# Example invocation
./auto-bump --project Cloud:Tools --package cf-cli --anitya-id 385503
```

Running with no target arguments (`--config`, `--user`, or `--project`/`--package`) defaults to the packages maintained by the authenticated OBS user (`osc whois`), so `./auto-bump --dry-run` works out of the box.

You can view the full list of arguments with:
```bash
./auto-bump --help
```

## Development

Set up the project:
```bash
uv sync
```

Run tests and linting:
```bash
make test
```
