ARG PYTHON_IMAGE
ARG FLINK_IMAGE
ARG CATALOG_IMAGE
FROM ${PYTHON_IMAGE} AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-17-jre-headless ca-certificates git curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY delivery/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes -r /tmp/requirements.lock

# 141MB of SHA256-locked JVM artifacts, fetched over the network. They depend on
# jars.lock.json and on nothing else, so they get a layer of their own, above the
# source. Downloading them in the same step as `streaming.generate` - below every
# COPY - meant a one-character Python change re-fetched all of them on a cache
# that never survived the runner anyway. This layer now changes only when the
# lock does. `**/jars` is in .dockerignore, so the source COPYs below cannot
# shadow what this writes.
COPY delivery/__init__.py delivery/dependencies.py delivery/jars.lock.json /app/delivery/
RUN python -m delivery.dependencies

COPY streaming /app/streaming
COPY catalog /app/catalog
COPY marketplace /app/marketplace
COPY delivery /app/delivery
COPY infra /app/infra
COPY data /app/data
COPY stream_kpi_dash_g2.py analytics_answers.py dashboard_theme.css logo.jpg /app/
RUN python -m streaming.generate
CMD ["python", "-m", "uvicorn", "streaming.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM ${FLINK_IMAGE} AS flink
COPY --from=app /app/streaming/jars/ /opt/flink/lib/
COPY --from=app /app/streaming/generated/03-medallion.sql /opt/nda/medallion.sql

FROM ${CATALOG_IMAGE} AS catalog
USER root
COPY --from=app /app/delivery/vendor/postgresql.jar /usr/lib/iceberg-rest/postgresql.jar
USER iceberg
ENTRYPOINT ["java", "-cp", "/usr/lib/iceberg-rest/postgresql.jar:/usr/lib/iceberg-rest/iceberg-rest-image-all.jar", "org.apache.iceberg.rest.RESTCatalogServer"]
