#!/usr/bin/env bash
# gen-certs.sh — Mint a CA, a server cert, and one client cert per
# machine ID supplied on the command line, into ./certs/.
#
# Used by the integration-test harness to set up mTLS without any
# external infrastructure.  Re-runs are destructive (the certs/ dir is
# wiped) so the test starts from a known state every time.
#
# Usage:
#   ./gen-certs.sh laptop-a laptop-b laptop-c rogue-client
#
# Output:
#   certs/ca.crt              CA cert (use as both server-CA and client-CA)
#   certs/ca.key              CA private key
#   certs/server.crt          Server cert (CN=localhost, SAN=localhost,127.0.0.1)
#   certs/server.key
#   certs/<id>.crt            Client cert (CN=<id>)
#   certs/<id>.key            Client private key
#   certs/<id>.sha256         SHA-256 of DER-encoded client cert
#   certs/fingerprints.tsv    machine_id<TAB>cert_sha256
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/certs"
rm -rf "$DIR"
mkdir -p "$DIR"
cd "$DIR"

# Minimal openssl config so SAN works without a config file.
SAN_CNF=$(cat <<'EOF'
[ req ]
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
DNS.1 = localhost
DNS.2 = sync-server
IP.1  = 127.0.0.1
EOF
)

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
