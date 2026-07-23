#!/bin/sh
set -eu

# TODO: at the moment MM_URL is a command line parameter not an environment
# variable. This means when user sets this parameter we don't have its value
# here. On the other hand user cannot set it via scripts/omegaclaw and when
# user runs OmegaClaw from the command line this proxy is not started
# automatically. At some point this parameter should be parsed in the
# entrypoint as well.
export MM_UPSTREAM_URL=${MM_URL:-https://chat.singularitynet.io}

# Upstreams resolved through a variable use this resolver, which has ipv6 off.
# Providers behind Cloudflare publish AAAA records; on a host with no IPv6 route
# nginx otherwise tries them and fails with "Network is unreachable". Prefer the
# system nameserver (first IPv4 one) so we follow the host's DNS.
DNS_RESOLVER=$(awk '/^nameserver/ && $2 !~ /:/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)
export DNS_RESOLVER=${DNS_RESOLVER:-1.1.1.1}

SUBST_VARS=$(grep -o '\${[A-Z_0-9]*}' /opt/nginx/nginx.conf.template | sort -u | tr '\n' ' ')
NGINX_CONFIG=/opt/nginx/nginx.conf
touch "${NGINX_CONFIG}"
chmod 0600 "${NGINX_CONFIG}"
envsubst "$SUBST_VARS" \
    < /opt/nginx/nginx.conf.template \
    > "${NGINX_CONFIG}"

nginx -c "${NGINX_CONFIG}"

