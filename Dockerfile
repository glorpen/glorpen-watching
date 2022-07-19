FROM python:3.10-alpine as base

LABEL org.opencontainers.image.authors="Arkadiusz Dzięgiel <arkadiusz.dziegiel@glorpen.pl>" \
      org.opencontainers.image.source="https://codeberg.org/glorpen/glorpen-watching" \
      org.opencontainers.image.licenses="GPL-3.0+"

FROM base as build

COPY ./src /root/pkg/src
COPY ./LICENSE.txt ./setup.cfg ./setup.py /root/pkg/

RUN cd /root/pkg \
    && pip install --root /image --compile --no-cache-dir ./[cron]

FROM base

COPY --from=build /image/usr/local/ /usr/local/

ENTRYPOINT ["python", "-m", "glorpen.watching.cron"]

LABEL org.opencontainers.image.version="1.0.1"
