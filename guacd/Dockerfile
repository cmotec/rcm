# guacd with its data directories owned by the guacd user (uid 1000).
#
# Docker initialises an empty named volume from the image directory it is mounted on, including
# owner and mode. Pre-creating /drives and /recordings here therefore makes the "drives" and
# "recordings" volumes writable for guacd on first start, and guacd never runs as root.
FROM guacamole/guacd:1.6.0

USER root
RUN mkdir -p /drives /recordings \
    && chown guacd:guacd /drives /recordings \
    && chmod 0750 /drives /recordings
USER guacd
