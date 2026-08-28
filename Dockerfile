FROM python:3.12-alpine

# tzdata so TZ=Asia/Tokyo resolves inside the container. (The script pins JST
# internally regardless, but log timestamps and the shell should agree.)
RUN apk add --no-cache tzdata

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo \
    KSL_STATE=/data/state.json \
    KSL_LOG=/data/ksl.log

WORKDIR /app
COPY ksl_watch.py /app/

# NOTE: deliberately no `VOLUME /data`. With a VOLUME declared, a plain
# `docker run` without -v silently creates a fresh anonymous volume each time,
# so the watcher would cold-start (and re-alert) on every recreate. Mount it
# explicitly instead -- docker-compose.yml does.

# Reports "unhealthy" in `docker ps` if the daemon stops keeping to its own
# published schedule. It tolerates deliberate rate-limit backoffs, which can
# legitimately run for hours.
HEALTHCHECK --interval=120s --timeout=15s --start-period=90s --retries=2 \
    CMD python3 /app/ksl_watch.py --healthcheck || exit 1

ENTRYPOINT ["python3", "/app/ksl_watch.py"]
CMD ["--daemon"]
