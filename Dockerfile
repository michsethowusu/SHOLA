# SHOLA on Coolify. Matches the other apps on that server, which all use the
# dockerfile build pack.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is needed by the entrypoint to fetch the word list on first boot.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
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

RUN chmod +x docker-entrypoint.sh
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "--workers", "3", "--threads", "4", "--timeout", "60", \
     "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", \
     "wsgi:app"]
