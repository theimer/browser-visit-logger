package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// writePEMKeyPair mints a self-signed cert + key and writes them as PEM
// files, returning their paths.  Good enough for buildTLS, which only
// needs LoadX509KeyPair to succeed and the CA file to parse.
func writePEMKeyPair(t *testing.T, dir, name string) (certPath, keyPath string) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: name},
		NotBefore:    time.Unix(0, 0),
		NotAfter:     time.Unix(1<<31-1, 0),
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certPath = filepath.Join(dir, name+".crt")
	keyPath = filepath.Join(dir, name+".key")
	writePEM(t, certPath, "CERTIFICATE", der)
	writePEM(t, keyPath, "RSA PRIVATE KEY", x509.MarshalPKCS1PrivateKey(key))
	return certPath, keyPath
}

func writePEM(t *testing.T, path, blockType string, der []byte) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := pem.Encode(f, &pem.Block{Type: blockType, Bytes: der}); err != nil {
		t.Fatal(err)
	}
}

func TestBuildTLSSuccess(t *testing.T) {
	dir := t.TempDir()
	cert, key := writePEMKeyPair(t, dir, "server")
	ca, _ := writePEMKeyPair(t, dir, "ca")
	creds, err := buildTLS(cert, key, ca)
	if err != nil {
		t.Fatalf("buildTLS: %v", err)
	}
	if creds == nil {
		t.Fatal("nil creds")
	}
}

func TestBuildTLSMissingKeyPair(t *testing.T) {
	dir := t.TempDir()
	ca, _ := writePEMKeyPair(t, dir, "ca")
	if _, err := buildTLS(filepath.Join(dir, "nope.crt"),
		filepath.Join(dir, "nope.key"), ca); err == nil {
		t.Fatal("expected LoadX509KeyPair error")
	}
}

func TestBuildTLSMissingCA(t *testing.T) {
	dir := t.TempDir()
	cert, key := writePEMKeyPair(t, dir, "server")
	if _, err := buildTLS(cert, key, filepath.Join(dir, "nope-ca.crt")); err == nil {
		t.Fatal("expected CA read error")
	}
}
