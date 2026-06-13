// Package auth pins each gRPC call's claimed machine_id to the
// client cert that's actually presented during mTLS.
//
// An enrolled_machines SQLite DB on the VM stores
//
//	(machine_id TEXT PRIMARY KEY, cert_sha256 TEXT NOT NULL)
//
// Operators add rows via browser-visit-tools/enroll_machine.py. The
// interceptor below extracts the peer cert from the gRPC context,
// SHA-256s it, and looks the row up.  The request's claimed
// machine_id (a field on every Push/Pull message) must match.
package auth

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"

	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/logmw"

	_ "modernc.org/sqlite"
)

type Enrolled struct{ db *sql.DB }

func OpenEnrolled(path string) (*Enrolled, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS enrolled_machines (
		machine_id  TEXT PRIMARY KEY,
		cert_sha256 TEXT NOT NULL,
		enrolled_at TEXT NOT NULL
	)`); err != nil {
		db.Close()
		return nil, err
	}
	return &Enrolled{db: db}, nil
}

func (e *Enrolled) Close() error { return e.db.Close() }

// Lookup returns the enrolled machine_id for the supplied cert
// fingerprint.  Empty string + nil error means "not enrolled".
func (e *Enrolled) Lookup(fp string) (string, error) {
	row := e.db.QueryRow(`SELECT machine_id FROM enrolled_machines WHERE cert_sha256 = ?`, fp)
	var id string
	if err := row.Scan(&id); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return "", nil
		}
		return "", err
	}
	return id, nil
}

type ctxKey struct{}

// ContextWithMachineID returns a child context carrying the
// cert-derived machine_id.  The interceptors use it to publish the
// authenticated identity; handlers read it back via CrossCheck /
// AuthenticatedMachineID.  Exported so tests can construct a context
// that looks like one a successful auth pass produced.
func ContextWithMachineID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, ctxKey{}, id)
}

func machineIDFromCtx(ctx context.Context) (string, bool) {
	v, ok := ctx.Value(ctxKey{}).(string)
	return v, ok
}

// AuthenticatedMachineID is the public accessor for handlers that need
// to compare the cert-derived identity against the request payload.
func AuthenticatedMachineID(ctx context.Context) (string, bool) {
	return machineIDFromCtx(ctx)
}

func resolveID(ctx context.Context, e *Enrolled) (string, error) {
	p, ok := peer.FromContext(ctx)
	if !ok || p.AuthInfo == nil {
		return "", status.Error(codes.Unauthenticated, "no peer info")
	}
	tlsInfo, ok := p.AuthInfo.(credentials.TLSInfo)
	if !ok {
		return "", status.Error(codes.Unauthenticated, "non-TLS connection")
	}
	chains := tlsInfo.State.VerifiedChains
	if len(chains) == 0 || len(chains[0]) == 0 {
		return "", status.Error(codes.Unauthenticated, "no verified client cert")
	}
	leaf := chains[0][0]
	sum := sha256.Sum256(leaf.Raw)
	fp := hex.EncodeToString(sum[:])
	id, err := e.Lookup(fp)
	if err != nil {
		return "", status.Errorf(codes.Internal, "enrolled lookup: %v", err)
	}
	if id == "" {
		return "", status.Errorf(codes.PermissionDenied, "client cert %s not enrolled", fp[:16])
	}
	return id, nil
}

// UnaryInterceptor verifies the mTLS cert against the enrolled DB and
// attaches the authoritative machine_id to the context.  Also writes
// the id into logmw's caller slot so the outer logging interceptor
// can include it in its log line.
func UnaryInterceptor(e *Enrolled) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		id, err := resolveID(ctx, e)
		if err != nil {
			return nil, err
		}
		logmw.SetCaller(ctx, id)
		return handler(ContextWithMachineID(ctx, id), req)
	}
}

// StreamInterceptor is the streaming-RPC twin of UnaryInterceptor.
func StreamInterceptor(e *Enrolled) grpc.StreamServerInterceptor {
	return func(srv interface{}, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		id, err := resolveID(ss.Context(), e)
		if err != nil {
			return err
		}
		logmw.SetCaller(ss.Context(), id)
		return handler(srv, &wrappedStream{ServerStream: ss, ctx: ContextWithMachineID(ss.Context(), id)})
	}
}

type wrappedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (w *wrappedStream) Context() context.Context { return w.ctx }

// CrossCheck returns nil iff the cert-derived id matches the
// request-claimed id.  Handlers call this on every RPC.
func CrossCheck(ctx context.Context, claimed string) error {
	id, ok := machineIDFromCtx(ctx)
	if !ok {
		return status.Error(codes.Unauthenticated, "no authenticated machine id")
	}
	if id != claimed {
		return status.Errorf(codes.PermissionDenied,
			"cert identity %q does not match claimed machine_id %q", id, claimed)
	}
	return nil
}

// _ is a placeholder so the file builds even when callers don't use
// fmt directly.
var _ = fmt.Sprintf
