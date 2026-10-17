#!/bin/sh
# TLS for the web container (runs before nginx starts, as the nginx user).
#
# Certificate: /etc/nginx/certs/tls.crt + tls.key when present (your own, e.g. from a CA or ACME).
# Otherwise a self-signed certificate for the APP_URL host name (or TLS_SELF_SIGNED_NAME) is created
# and kept in /etc/nginx/certs (self-signed.crt/.key), so it stays the same across restarts; it is
# renewed 30 days before it expires. nginx reads /tmp/tls/tls.crt and tls.key (links to the chosen pair).
set -eu

CERT_DIR=/etc/nginx/certs
OUT=/tmp/tls
mkdir -p "$OUT"

name="${TLS_SELF_SIGNED_NAME:-}"
if [ -z "$name" ]; then
    name=$(printf '%s' "${APP_URL:-}" | sed -E 's#^[A-Za-z]+://##; s#^\[##; s#[]/:].*$##')
fi
[ -n "$name" ] || name=localhost

# A broken certificate of your own must not keep nginx from starting: check it, else fall back.
own=0
if [ -e "$CERT_DIR/tls.crt" ] || [ -e "$CERT_DIR/tls.key" ]; then
    if [ ! -r "$CERT_DIR/tls.crt" ] || [ ! -r "$CERT_DIR/tls.key" ]; then
        echo "tls: ERROR $CERT_DIR/tls.crt and tls.key must both exist and be readable for uid $(id -u); using the self-signed certificate"
    elif ! crt_pub=$(openssl x509 -noout -pubkey -in "$CERT_DIR/tls.crt" 2>/dev/null) \
        || ! key_pub=$(openssl pkey -pubout -in "$CERT_DIR/tls.key" 2>/dev/null); then
        echo "tls: ERROR $CERT_DIR/tls.crt or tls.key is not a valid PEM certificate / unencrypted private key; using the self-signed certificate"
    elif [ "$crt_pub" != "$key_pub" ]; then
        echo "tls: ERROR $CERT_DIR/tls.key does not belong to tls.crt; using the self-signed certificate"
    else
        own=1
    fi
fi

if [ "$own" = 1 ]; then
    ln -sf "$CERT_DIR/tls.crt" "$OUT/tls.crt"
    ln -sf "$CERT_DIR/tls.key" "$OUT/tls.key"
    echo "tls: using the certificate in $CERT_DIR: $(openssl x509 -noout -subject -enddate -in "$CERT_DIR/tls.crt" | tr '\n' ' ')"
    openssl x509 -checkend 0 -noout -in "$CERT_DIR/tls.crt" >/dev/null 2>&1 \
        || echo "tls: WARNING $CERT_DIR/tls.crt has expired"
else
    dir="$CERT_DIR"
    if ! (: > "$CERT_DIR/.write-test") 2>/dev/null; then
        dir="$OUT"
        echo "tls: $CERT_DIR is not writable for uid $(id -u); the self-signed certificate is recreated at every start"
    else
        rm -f "$CERT_DIR/.write-test"
    fi
    if [ ! -s "$dir/self-signed.crt" ] || [ ! -s "$dir/self-signed.key" ] \
        || ! openssl x509 -checkend 2592000 -noout -in "$dir/self-signed.crt" >/dev/null 2>&1 \
        || ! openssl x509 -noout -subject -in "$dir/self-signed.crt" 2>/dev/null | grep -q "CN *= *$name\$"; then
        case "$name" in
            *:*) san="IP:$name" ;;
            *[!0-9.]*) san="DNS:$name" ;;
            *) san="IP:$name" ;;
        esac
        umask 077
        openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=$name" \
            -addext "subjectAltName=$san,DNS:localhost,IP:127.0.0.1" \
            -addext "keyUsage=digitalSignature,keyEncipherment" -addext "extendedKeyUsage=serverAuth" \
            -keyout "$dir/self-signed.key" -out "$dir/self-signed.crt" >/dev/null 2>&1
        echo "tls: created a self-signed certificate for $name"
    fi
    ln -sf "$dir/self-signed.crt" "$OUT/tls.crt"
    ln -sf "$dir/self-signed.key" "$OUT/tls.key"
    echo "tls: self-signed, $(openssl x509 -noout -fingerprint -sha256 -in "$dir/self-signed.crt")"
    echo "tls: put tls.crt (full chain) and tls.key into $CERT_DIR to use your own certificate"
fi
