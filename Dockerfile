# Recertia API (console) and worker image.
#
# Base is the official python:3.12-slim *index* digest (multi-arch), resolved
# 2026-11-17 from registry-1.docker.io (tag 3.12-slim → Python 3.12.16,
# debian trixie slim). Refresh: see docs/architecture/app-containers.md.
#
# This repo has no lockfile (no uv.lock / poetry.lock / requirements.txt).
# Direct deps come from pyproject.toml extras [api,postgres] (version floors).
# Transitive pins follow PyPI at build time; say so when you refresh.
#
# Not the execution sandbox image (that stays stock python:3.12-slim, allowlisted
# in recertia.solver.container). Not the pin-dockerfile-base practice skill.

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# 65532 is distinct from the sandbox nobody (65534) used by solver/container.py.
RUN groupadd --gid 65532 recertia \
    && useradd --uid 65532 --gid recertia --create-home --home-dir /home/recertia \
        --shell /usr/sbin/nologin recertia

WORKDIR /tmp/recertia-build
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY contracts ./contracts
RUN python -m pip install --no-compile ".[api,postgres]" \
    && rm -rf /tmp/recertia-build /root/.cache/pip

# Seed library + soak helper. Compose may bind-mount checkout copies over these.
COPY --chown=recertia:recertia skills /home/recertia/skills
COPY --chown=recertia:recertia policy /home/recertia/policy
COPY --chown=recertia:recertia facts /home/recertia/facts
COPY --chown=recertia:recertia scripts /opt/recertia/scripts
COPY --chown=recertia:recertia data /opt/recertia/data

COPY docker/entrypoint.sh /usr/local/bin/recertia-entrypoint
RUN chmod 0755 /usr/local/bin/recertia-entrypoint && command -v setpriv

WORKDIR /home/recertia
EXPOSE 8080
# Runs as root only long enough to chown the data volume, then drops to uid 65532.
ENTRYPOINT ["/usr/local/bin/recertia-entrypoint"]
CMD ["python", "-m", "uvicorn", "recertia.api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
