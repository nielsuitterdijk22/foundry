# foundry-proxy: the agent's only way out. tinyproxy enforces a domain allowlist;
# socat forwards :8080 to the model server on the host.
FROM alpine:3.23
RUN apk add --no-cache tinyproxy socat
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
