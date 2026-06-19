#!/usr/bin/env bash
# gen-certs.sh — Mint a CA, a server cert, and one client cert per
# machine ID supplied on the command line, into ./certs/.
#
# Used by the integration-test harness to set up mTLS without any
# external infrastructure.  A normal (non --add-client) run is
# destructive — the certs/ dir is wiped and a fresh CA is minted — so
# the test starts from a known state every time.
#
# Usage:
#   ./gen-certs.sh [--server-host HOST]... <machine-id>...   # full run
#   ./gen-certs.sh --add-client <machine-id>...              # incremental
#
#   --server-host HOST   Add a real DNS name or IP to the server cert's
#                        SAN so the cert is valid for a VM reachable at
#                        that address.  Repeatable.  Auto-detected as an
#                        IP (added as IP.n) or a DNS name (added as DNS.n).
#                        Omit it for the integration tests — without it the
#                        server cert is the original localhost-only cert.
#
#   --add-client ID      INCREMENTAL, NON-DESTRUCTIVE.  Sign one more
#                        client cert with the EXISTING certs/ca.{crt,key}
#                        without wiping certs/ or touching the CA / server
#                        cert.  Repeatable.  This is how you add a laptop
#                        later (e.g. a new machine) without re-issuing
#                        everything.  Requires certs/ca.crt + certs/ca.key
#                        from the original run.  Cannot be combined with
#                        --server-host or positional machine-ids.
#
# Examples:
#   ./gen-certs.sh laptop-a laptop-b laptop-c rogue-client      # test harness
#   ./gen-certs.sh --server-host bvl-vm.example.com my-mbp      # production
#   ./gen-certs.sh --server-host 203.0.113.7 my-mbp other-mbp   # by IP
#   ./gen-certs.sh --add-client my-new-mbp                      # add a laptop
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
#   certs/fingerprints.tsv    machine_id<TAB>cert_sha256 (appended in
#                             --add-client mode, rewritten otherwise)
set -euo pipefail

# Parse leading flags; the rest are positional machine IDs.  (The test
# harness calls this with machine IDs only, so the default output is
# unchanged when no flag is given.)
SERVER_HOSTS=()
ADD_CLIENTS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --server-host)
            [ $# -ge 2 ] || { echo "error: --server-host needs a value" >&2; exit 2; }
            SERVER_HOSTS+=("$2"); shift 2 ;;
        --server-host=*)
            SERVER_HOSTS+=("${1#*=}"); shift ;;
        --add-client)
            [ $# -ge 2 ] || { echo "error: --add-client needs a value" >&2; exit 2; }
            ADD_CLIENTS+=("$2"); shift 2 ;;
        --add-client=*)
            ADD_CLIENTS+=("${1#*=}"); shift ;;
        --) shift; break ;;
        -*) echo "error: unknown flag: $1" >&2; exit 2 ;;
        *) break ;;
    esac
done

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/certs"

# Mint one client cert into the current directory (must hold ca.crt,
# ca.key and client_ext.cnf).  Appends its fingerprint to fingerprints.tsv.
# OpenSSL reuses ca.srl across calls, so -CAcreateserial is safe to repeat.
mint_client() {
    local id="$1" fp
    openssl genrsa -out "${id}.key" 2048 2>/dev/null
    openssl req -new -key "${id}.key" -subj "/CN=${id}" -out "${id}.csr"
    openssl x509 -req -days 3650 -in "${id}.csr" -CA ca.crt -CAkey ca.key \
      -CAcreateserial -extfile client_ext.cnf -extensions client_ext \
      -out "${id}.crt" 2>/dev/null
    rm "${id}.csr"
    fp=$(openssl x509 -in "${id}.crt" -outform DER | openssl dgst -sha256 -hex | awk '{print $NF}')
    echo "$fp" > "${id}.sha256"
    printf '%s\t%s\n' "$id" "$fp" >> fingerprints.tsv
    chmod 600 "${id}.key"
}

# OpenSSL 3.x rejects extfiles without a named section, so use a small
# inline file with a [client_ext] block.
CLIENT_EXT='[ client_ext ]
extendedKeyUsage = clientAuth
keyUsage = digitalSignature'

# ----------------------------------------------------------------------
# Incremental mode: add client cert(s) against the existing CA, leaving
# certs/, the CA and the server cert untouched.
# ----------------------------------------------------------------------
if [ ${#ADD_CLIENTS[@]} -gt 0 ]; then
    [ ${#SERVER_HOSTS[@]} -eq 0 ] || {
        echo "error: --server-host is not valid with --add-client "\
"(the server cert is not regenerated in incremental mode)" >&2; exit 2; }
    [ $# -eq 0 ] || {
        echo "error: positional machine-ids are not valid with --add-client" >&2
        exit 2; }
    [ -f "$DIR/ca.crt" ] && [ -f "$DIR/ca.key" ] || {
        echo "error: --add-client needs an existing CA at $DIR/ca.{crt,key} "\
"from the original run; none found" >&2; exit 2; }
    cd "$DIR"
    echo "$CLIENT_EXT" > client_ext.cnf
    [ -f fingerprints.tsv ] || : > fingerprints.tsv
    for id in "${ADD_CLIENTS[@]}"; do
        mint_client "$id"
        echo "added client cert ${id}.crt (signed by existing CA)"
    done
    rm client_ext.cnf
    exit 0
fi

# ----------------------------------------------------------------------
# Full run: wipe, mint a fresh CA + server cert + the listed clients.
# ----------------------------------------------------------------------
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
echo "$CLIENT_EXT" > client_ext.cnf
: > fingerprints.tsv
for id in "$@"; do
    mint_client "$id"
done
rm client_ext.cnf

chmod 600 *.key
echo "wrote certs to $DIR"
cat fingerprints.tsv
