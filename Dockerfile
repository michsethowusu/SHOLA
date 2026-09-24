# SHOLA on Coolify. Matches the other apps on that server, which all use the
# dockerfile build pack.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is needed by the entrypoint to fetch the word list on first boot.
#
# pg_dump and mysqldump are for `shola backup`, which dumps the other
# applications' managed databases. Coolify's own scheduled backup records
# success when its helper container is missing and nothing has left the
# machine; this command uploads directly and verifies the stored object.
#
# postgresql-client comes from PGDG, not Debian. pg_dump refuses a server newer
# than itself, Debian bookworm ships client 15, and the servers here are
# Postgres 16 and 18 - the distribution package would fail on both of the
# databases that most need backing up. mariadb-client provides mysqldump.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
      "http://apt.postgresql.org/pub/repos/apt" \
      "$(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-18 \
      mariadb-client \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The database lives here. Mount it as a volume or a redeploy wipes every
# volunteer and every answer they have given.
RUN mkdir -p /app/instance
VOLUME ["/app/instance"]

# Coolify passes the commit it is building; bake it in so /healthz can report
# which build is actually serving.
ARG SOURCE_COMMIT=unknown
ENV SHOLA_BUILD=$SOURCE_COMMIT

ENV SHOLA_DATABASE_URL=sqlite:////app/instance/shola.db
EXPOSE 8000

# So a container that cannot serve says so, instead of sitting in Coolify as
# "running:unknown" while it crash-loops. /healthz answers 503 when it cannot
# reach the database, and curl -f turns that into a non-zero exit.
#
# start-period is generous because boot is not instant: the schema check, the
# index check and the language backfill all run before the first request, over
# a 660 MB database. Failing a container for being slow to start is how you
# turn a slow boot into an outage.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz > /dev/null || exit 1

RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "3", "--threads", "4", "--timeout", "60", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", \
     "wsgi:app"]
