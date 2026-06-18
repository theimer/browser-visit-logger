#!/usr/bin/env bash
# gen-certs.sh — Mint a CA, a server cert, and one client cert per
# machine ID supplied on the command line, into ./certs/.
#
# Used by the integration-test harness to set up mTLS without any
# external infrastructure.  Re-runs are destructive (the certs/ dir is
# wiped) so the test starts from a known state every time.
#
# Usage:
#   ./gen-certs.sh [--server-host HOST]... <machine-id>...
#
#   --server-host HOST   Add a real DNS name or IP to the server cert's
#                        SAN so the cert is valid for a VM reachable at
#                        that address.  Repeatable.  Auto-detected as an
#                        IP (added as IP.n) or a DNS name (added as DNS.n).
#                        Omit it for the integration tests — without it the
#                        server cert is the original localhost-only cert.
#
# Examples:
#   ./gen-certs.sh laptop-a laptop-b laptop-c rogue-client      # test harness
#   ./gen-certs.sh --server-host bvl-vm.example.com my-mbp      # production
#   ./gen-certs.sh --server-host 203.0.113.7 my-mbp other-mbp   # by IP
#
# Output:
#   certs/ca.crt              CA cert (use as both server-CA and client-CA)
#   certs/ca.key              CA private key
#   certs/server.crt          Server cert (CN=localhost; SAN always
#                             includes localhost/127.0.0.1 plus any
#                             --server-host values)
#   certs/server.key
#   certs/<id>.crt            Client cert (CN=<id>)
#   certs/<id>.key            Client private key
#   certs/<id>.sha256         SHA-256 of DER-encoded client cert
#   certs/fingerprints.tsv    machine_id<TAB>cert_sha256
set -euo pipefail

# Parse leading --server-host flags; the rest are positional machine IDs.
# (The test harness calls this with machine IDs only, so the default
# output is unchanged when no flag is given.)
SERVER_HOSTS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --server-host)
            [ $# -ge 2 ] || { echo "error: --server-host needs a value" >&2; exit 2; }
            SERVER_HOSTS+=("$2"); shift 2 ;;
        --server-host=*)
            SERVER_HOSTS+=("${1#*=}"); shift ;;
        --) shift; break ;;
        -*) echo "error: unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/certs"
rm -rf "$DIR"
mkdir -p "$DIR"
cd "$DIR"

# Build the [ alt_names ] block: the localhost defaults the tests rely on,
# plus one entry per --server-host (numbered after the defaults, IP vs DNS
# detected from the value's shape).
ALT_NAMES=$'DNS.1 = localhost\nDNS.2 = sync-server\nIP.1  = 127.0.0.1'
dns_n=2
ip_n=1
for host in "${SERVER_HOSTS[@]:-}"; do
    [ -n "$host" ] || continue
    if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        ip_n=$((ip_n + 1)); ALT_NAMES+=$'\n'"IP.${ip_n}  = ${host}"
    else
        dns_n=$((dns_n + 1)); ALT_NAMES+=$'\n'"DNS.${dns_n} = ${host}"
    fi
done

# Minimal openssl config so SAN works without a config file.
SAN_CNF="[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = req_ext

[ dn ]
CN = localhost

[ req_ext ]
subjectAltName = @alt_names
extendedKeyUsage = serverAuth

[ alt_names ]
${ALT_NAMES}"

# 1) CA
openssl genrsa -out ca.key 2048 2>/dev/null
openssl req -new -x509 -days 3650 -key ca.key -subj "/CN=bvl-test-ca" -out ca.crt

# 2) Server cert
echo "$SAN_CNF" > server.cnf
openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -config server.cnf -out server.csr
openssl x509 -req -days 3650 -in server.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -extfile server.cnf -extensions req_ext -out server.crt 2>/dev/null
rm server.csr server.cnf

# 3) Client certs
# OpenSSL 3.x rejects extfiles without a named section, so use a small
# inline file with a [client_ext] block.
cat > client_ext.cnf <<'EOF'
[ client_ext ]
extendedKeyUsage = clientAuth
keyUsage = digitalSignature
EOF

: > fingerprints.tsv
for id in "$@"; do
    openssl genrsa -out "${id}.key" 2048 2>/dev/null
    openssl req -new -key "${id}.key" -subj "/CN=${id}" -out "${id}.csr"
    openssl x509 -req -days 3650 -in "${id}.csr" -CA ca.crt -CAkey ca.key \
      -CAcreateserial -extfile client_ext.cnf -extensions client_ext \
      -out "${id}.crt" 2>/dev/null
    rm "${id}.csr"
    fp=$(openssl x509 -in "${id}.crt" -outform DER | openssl dgst -sha256 -hex | awk '{print $NF}')
    echo "$fp" > "${id}.sha256"
    printf '%s\t%s\n' "$id" "$fp" >> fingerprints.tsv
done
rm client_ext.cnf

chmod 600 *.key
echo "wrote certs to $DIR"
cat fingerprints.tsv
