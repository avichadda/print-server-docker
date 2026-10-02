FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="CUPS Print Server" \
      org.opencontainers.image.description="A small, configurable CUPS print server for driverless IPP printers" \
      org.opencontainers.image.source="https://github.com/avichadda/print-server-docker" \
      org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        avahi-daemon \
        cups \
        cups-client \
        cups-filters \
        cups-ipp-utils \
        dbus-daemon \
        libnss-mdns \
        python3 \
        python3-flask \
        python3-waitress \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/cups/cupsd.conf

COPY config/cupsd.conf /etc/cups/cupsd.conf
COPY manager /opt/print-server/manager
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/healthcheck.sh /usr/local/bin/healthcheck.sh

RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh \
    && mkdir -p /data /run/cups /var/log/cups /var/spool/cups

ENV PYTHONPATH=/opt/print-server

EXPOSE 631/tcp 8080/tcp 5353/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
