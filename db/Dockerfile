# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

FROM public.ecr.aws/docker/library/python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# psql-client — run_migration.sh 가 Aurora 에서 init/*.sql 을 직접 실행할 때 사용.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Optional: copy custom CA certs if present (for corporate proxy)
COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

RUN uv pip install --system alembic asyncpg sqlalchemy

COPY alembic.ini env.py ./
COPY versions/ ./versions/
# init/*.sql — run_migration.sh (ECS RunTask) 가 직접 실행
COPY init/ ./init/
COPY run_migration.sh ./
RUN chmod +x run_migration.sh

# Run as a non-root user.
# EN: Even a one-shot migration Job benefits from least-privilege. Running as
#     a dedicated UID also lets the cluster enforce PodSecurity `restricted`
#     (`runAsNonRoot: true`) without an explicit `securityContext` override.
#     `alembic` was installed system-wide above, so it is on PATH for any
#     UID; the chown ensures the migration binary is executable by `appuser`.
# KO: 일회성 migration Job 이라도 최소 권한 원칙을 따릅니다. 전용 UID 로
#     실행하면 클러스터의 PodSecurity `restricted` (`runAsNonRoot: true`)
#     정책을 별도 `securityContext` override 없이 만족합니다. `alembic` 은
#     위에서 system-wide 로 설치되어 어떤 UID 에서도 PATH 에 있으며,
#     chown 으로 `appuser` 가 마이그레이션 스크립트를 실행할 수 있도록 합니다.
RUN groupadd --system --gid 10001 appgroup \
    && useradd  --system --uid 10001 --gid appgroup --create-home --home-dir /home/appuser appuser \
    && chown -R appuser:appgroup /app
USER 10001:10001

CMD ["./run_migration.sh"]
