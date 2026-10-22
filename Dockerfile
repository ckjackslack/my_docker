# Python development image for the `app` (and `celery`) services.
# Build context is the repository root; APP_DIR selects the application directory.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG TZ=UTC
ARG APP_DIR=app
# 1 = also install the developer tool belt (linters, poetry, ansible, ssh/rsync, vim).
ARG INSTALL_DEV_TOOLS=1
ARG USER_UID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=${TZ}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git tzdata \
    && if [ "$INSTALL_DEV_TOOLS" = "1" ]; then \
         apt-get install -y --no-install-recommends openssh-client rsync vim; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
    && echo "$TZ" > /etc/timezone

RUN if [ "$INSTALL_DEV_TOOLS" = "1" ]; then \
      pip install black flake8 isort mypy poetry ansible; \
    fi

RUN useradd --create-home --uid "$USER_UID" developer

WORKDIR /usr/src/app

# Dependencies first so they are cached until requirements.txt changes.
COPY ${APP_DIR}/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY --chown=developer:developer ${APP_DIR}/ ./
RUN mkdir -p staticfiles && chown developer:developer staticfiles

USER developer

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "main.py"]
