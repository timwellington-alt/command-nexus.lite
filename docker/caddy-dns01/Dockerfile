# Caddy with the Cloudflare DNS plugin — for DNS-01 ACME challenges
# in LAN-only deployments that still want a real Let's Encrypt cert
# via split-horizon DNS.
#
# Build args let you swap the DNS provider: pass DNS_PLUGIN=digitalocean
# (or route53, hetzner, powerdns, gandi, etc.) to pick a different one.
# Full list: https://github.com/caddy-dns
#
# Default: Cloudflare, since it's the most common free-tier DNS host
# districts use. Needs CLOUDFLARE_API_TOKEN with Zone:DNS:Edit scope.
ARG DNS_PLUGIN=cloudflare
FROM caddy:2-builder AS builder
ARG DNS_PLUGIN
RUN xcaddy build --with github.com/caddy-dns/${DNS_PLUGIN}

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
