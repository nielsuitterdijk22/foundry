#!/bin/sh
# MODEL_PORT: port of the model server on the host
socat TCP-LISTEN:${MODEL_PORT:-8080},fork,reuseaddr TCP:host.docker.internal:${MODEL_PORT:-8080} &
exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
