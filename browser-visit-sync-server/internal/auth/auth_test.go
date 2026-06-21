package auth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"math/big"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

func openTestEnrolled(t *testing.T) *Enrolled {
	t.Helper()
	e, err := OpenEnrolled(filepath.Join(t.TempDir(), "enrolled.db"))
	if err != nil {
		t.Fatalf("OpenEnrolled: %v", err)
	}
	t.Cleanup(func() { e.Close() })
	return e
}

func mintCert(t *testing.T, cn string) *x509.Certificate {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Unix(0, 0),
		NotAfter:     time.Unix(1<<31-1, 0),
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

func fingerprint(c *x509.Certificate) string {
	sum := sha256.Sum256(c.Raw)
	return hex.EncodeToString(sum[:])
}

func ctxWithCert(cert *x509.Certificate) context.Context {
	p := &peer.Peer{
		AuthInfo: credentials.TLSInfo{
			State: tls.ConnectionState{
				VerifiedChains: [][]*x509.Certificate{{cert}},
			},
		},
	}
	return peer.NewContext(context.Background(), p)
}

func TestOpenEnrolledBadPath(t *testing.T) {
	// A path under a non-existent, uncreatable directory makes the
	// CREATE TABLE Exec fail.
	_, err := OpenEnrolled("/nonexistent-root-xyz/sub/enrolled.db")
	if err == nil {
		t.Fatal("expected open error")
	}
}

func TestLookup(t *testing.T) {
	e := openTestEnrolled(t)
	cert := mintCert(t, "laptop-a")
	fp := fingerprint(cert)
	if _, err := e.db.Exec(
		"INSERT INTO enrolled_machines (machine_id, cert_sha256, enrolled_at) VALUES (?,?,?)",
		"laptop-a", fp, "now"); err != nil {
		t.Fatal(err)
	}
	id, err := e.Lookup(fp)
	if err != nil || id != "laptop-a" {
		t.Fatalf("Lookup hit: id=%q err=%v", id, err)
	}
	// Miss → empty, nil.
	id, err = e.Lookup("deadbeef")
	if err != nil || id != "" {
		t.Fatalf("Lookup miss: id=%q err=%v", id, err)
	}
}

func TestLookupQueryError(t *testing.T) {
	e := openTestEnrolled(t)
	e.db.Close() // subsequent query errors (not ErrNoRows)
	if _, err := e.Lookup("x"); err == nil {
		t.Fatal("expected query error")
	}
}

// TestLookupAfterRevoke documents the live-revocation semantics: Lookup
// re-queries the DB on every call (no caching), so deleting an enrolled
// row through the same handle takes effect on the next lookup without
// reopening the DB or restarting the server.
func TestLookupAfterRevoke(t *testing.T) {
	e := openTestEnrolled(t)
	cert := mintCert(t, "laptop-a")
	fp := fingerprint(cert)
	if _, err := e.db.Exec(
		"INSERT INTO enrolled_machines (machine_id, cert_sha256, enrolled_at) VALUES (?,?,?)",
		"laptop-a", fp, "now"); err != nil {
		t.Fatal(err)
	}
	if id, err := e.Lookup(fp); err != nil || id != "laptop-a" {
		t.Fatalf("pre-revoke Lookup: id=%q err=%v", id, err)
	}
	// Revoke == delete the row (what enroll_machine.py --revoke does).
	if _, err := e.db.Exec(
		"DELETE FROM enrolled_machines WHERE cert_sha256 = ?", fp); err != nil {
		t.Fatal(err)
	}
	if id, err := e.Lookup(fp); err != nil || id != "" {
		t.Fatalf("post-revoke Lookup should miss: id=%q err=%v", id, err)
	}
}

func TestResolveIDNoPeer(t *testing.T) {
	e := openTestEnrolled(t)
	_, err := resolveID(context.Background(), e)
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want Unauthenticated, got %v", err)
	}
}

func TestResolveIDNonTLS(t *testing.T) {
	e := openTestEnrolled(t)
	ctx := peer.NewContext(context.Background(), &peer.Peer{AuthInfo: nonTLSAuth{}})
	_, err := resolveID(ctx, e)
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want Unauthenticated, got %v", err)
	}
}

type nonTLSAuth struct{}

func (nonTLSAuth) AuthType() string { return "fake" }

func TestResolveIDNoVerifiedChain(t *testing.T) {
	e := openTestEnrolled(t)
	p := &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{}}}
	ctx := peer.NewContext(context.Background(), p)
	_, err := resolveID(ctx, e)
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want Unauthenticated, got %v", err)
	}
}

func TestResolveIDNotEnrolled(t *testing.T) {
	e := openTestEnrolled(t)
	cert := mintCert(t, "ghost")
	_, err := resolveID(ctxWithCert(cert), e)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("want PermissionDenied, got %v", err)
	}
}

func TestResolveIDLookupError(t *testing.T) {
	e := openTestEnrolled(t)
	e.db.Close() // forces an internal error inside Lookup
	cert := mintCert(t, "x")
	_, err := resolveID(ctxWithCert(cert), e)
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal, got %v", err)
	}
}

func TestResolveIDSuccess(t *testing.T) {
	e := openTestEnrolled(t)
	cert := mintCert(t, "laptop-a")
	if _, err := e.db.Exec(
		"INSERT INTO enrolled_machines (machine_id, cert_sha256, enrolled_at) VALUES (?,?,?)",
		"laptop-a", fingerprint(cert), "now"); err != nil {
		t.Fatal(err)
	}
	id, err := resolveID(ctxWithCert(cert), e)
	if err != nil || id != "laptop-a" {
		t.Fatalf("resolveID success: id=%q err=%v", id, err)
	}
}

func TestCrossCheck(t *testing.T) {
	// No id in ctx → Unauthenticated.
	if status.Code(CrossCheck(context.Background(), "x")) != codes.Unauthenticated {
		t.Error("missing id should be Unauthenticated")
	}
	ctx := ContextWithMachineID(context.Background(), "laptop-a")
	// Match → nil.
	if err := CrossCheck(ctx, "laptop-a"); err != nil {
		t.Errorf("match should pass: %v", err)
	}
	// Mismatch → PermissionDenied.
	if status.Code(CrossCheck(ctx, "laptop-b")) != codes.PermissionDenied {
		t.Error("mismatch should be PermissionDenied")
	}
}

func TestAuthenticatedMachineID(t *testing.T) {
	if _, ok := AuthenticatedMachineID(context.Background()); ok {
		t.Error("empty ctx should report not-ok")
	}
	ctx := ContextWithMachineID(context.Background(), "laptop-a")
	id, ok := AuthenticatedMachineID(ctx)
	if !ok || id != "laptop-a" {
		t.Errorf("got (%q,%v)", id, ok)
	}
}

func enrolledWith(t *testing.T, cert *x509.Certificate, id string) *Enrolled {
	e := openTestEnrolled(t)
	if _, err := e.db.Exec(
		"INSERT INTO enrolled_machines (machine_id, cert_sha256, enrolled_at) VALUES (?,?,?)",
		id, fingerprint(cert), "now"); err != nil {
		t.Fatal(err)
	}
	return e
}

func TestUnaryInterceptor(t *testing.T) {
	cert := mintCert(t, "laptop-a")
	e := enrolledWith(t, cert, "laptop-a")
	intc := UnaryInterceptor(e)

	// Success: handler sees the id in context.
	var seen string
	_, err := intc(ctxWithCert(cert), nil, &grpc.UnaryServerInfo{},
		func(ctx context.Context, req interface{}) (interface{}, error) {
			id, _ := AuthenticatedMachineID(ctx)
			seen = id
			return "ok", nil
		})
	if err != nil || seen != "laptop-a" {
		t.Fatalf("unary success: err=%v seen=%q", err, seen)
	}

	// Failure: unenrolled cert short-circuits before the handler.
	ran := false
	_, err = intc(ctxWithCert(mintCert(t, "ghost")), nil, &grpc.UnaryServerInfo{},
		func(ctx context.Context, req interface{}) (interface{}, error) {
			ran = true
			return nil, nil
		})
	if err == nil || ran {
		t.Fatalf("unary should reject: err=%v ran=%v", err, ran)
	}
}

type fakeStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (f *fakeStream) Context() context.Context { return f.ctx }

func TestStreamInterceptor(t *testing.T) {
	cert := mintCert(t, "laptop-a")
	e := enrolledWith(t, cert, "laptop-a")
	intc := StreamInterceptor(e)

	var seen string
	err := intc(nil, &fakeStream{ctx: ctxWithCert(cert)}, &grpc.StreamServerInfo{},
		func(srv interface{}, ss grpc.ServerStream) error {
			id, _ := AuthenticatedMachineID(ss.Context())
			seen = id
			return nil
		})
	if err != nil || seen != "laptop-a" {
		t.Fatalf("stream success: err=%v seen=%q", err, seen)
	}

	ran := false
	err = intc(nil, &fakeStream{ctx: ctxWithCert(mintCert(t, "ghost"))}, &grpc.StreamServerInfo{},
		func(srv interface{}, ss grpc.ServerStream) error {
			ran = true
			return nil
		})
	if err == nil || ran {
		t.Fatalf("stream should reject: err=%v ran=%v", err, ran)
	}
}
