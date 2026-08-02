# obs-automation

Headless package bumping and automation for the openSUSE Build Service (OBS).
It monitors [Anitya](https://release-monitoring.org/) for upstream releases and automatically creates branched Submit Requests via a GitHub Actions pipeline.

## Features

- **Decoupled from GitHub Rate Limits:** Leverages the open-source Anitya ecosystem.
- **Resilient:** Uses HTTPx with Tenacity for network retries.
- **Stateless/Headless:** Runs completely isolated in CI without requiring local OS state.
- **Safe:** Branches the target project, ensuring human maintainers review the changes via Submit Requests.

## Usage

A convenient wrapper script is provided in the top directory to execute the tool locally. You must have [uv](https://github.com/astral-sh/uv) installed.

```bash
# Example invocation
./auto-bump --project Cloud:Tools --package cf-cli --anitya-id 385503
```

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
