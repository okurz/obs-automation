# Defines the tag for OBS and build script builds:
#!BuildTag: obs-automation:latest

FROM opensuse/tumbleweed
RUN zypper -n in \
    osc jq curl sed build git-core \
    obs-service-tar_scm obs-service-go_modules obs-service-set_version obs-service-recompress \
    python3-httpx python3-tenacity python3-typer python3-PyYAML python3-defusedxml && \
    zypper clean -a
