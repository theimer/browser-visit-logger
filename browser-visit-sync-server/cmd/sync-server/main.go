// sync-server is the gRPC service that mirrors laptop logs onto the
// EC2 VM and replays them into the canonical browser-visits DB.
//
// See ../../README.md for deployment.
package main

import (
	"crypto/tls"
	"crypto/x509"
	"flag"
	"log"
	"net"
	"os"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	syncpb "github.com/theimer/browser-visit-logger/browser-visit-sync-server/gen/syncpb"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/auth"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/logmw"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/server"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/store"
)

func main() {
	var (
		listen      = flag.String("listen", ":50443", "address to listen on")
		logRoot     = flag.String("log-root", "/var/lib/browser-visit-sync/logs", "directory of per-machine log mirrors")
		dbPath      = flag.String("db", "/var/lib/browser-visit-sync/browser-visits.db", "canonical SQLite DB path")
		enrolledDB  = flag.String("enrolled", "/var/lib/browser-visit-sync/enrolled_machines.db", "SQLite DB of enrolled (machine_id, cert fingerprint) pairs")
		tlsCert     = flag.String("tls-cert", "", "server cert PEM (omit with --insecure)")
		tlsKey      = flag.String("tls-key", "", "server key PEM")
		tlsClientCA = flag.String("tls-client-ca", "", "CA bundle that signs client certs")
		insecure    = flag.Bool("insecure", false, "disable TLS; for local dev only")
	)
	flag.Parse()

	st, err := store.Open(*logRoot, *dbPath)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer st.Close()

	enroller, err := auth.OpenEnrolled(*enrolledDB)
	if err != nil {
		log.Fatalf("open enrolled DB: %v", err)
	}
	defer enroller.Close()

	var opts []grpc.ServerOption
	if *insecure {
		log.Println("WARNING: running without TLS; do not use in production")
		// Even in insecure mode we want per-request logs.  No auth
		// interceptor here means handlers' CrossCheck calls will
		// fail; insecure mode is for never-deployed local dev only.
		opts = append(opts, grpc.ChainUnaryInterceptor(logmw.UnaryInterceptor()))
		opts = append(opts, grpc.ChainStreamInterceptor(logmw.StreamInterceptor()))
	} else {
		creds, err := buildTLS(*tlsCert, *tlsKey, *tlsClientCA)
		if err != nil {
			log.Fatalf("build TLS creds: %v", err)
		}
		opts = append(opts, grpc.Creds(creds))
		// Order matters: logmw must be the *outer* interceptor so it
		// runs even when auth rejects.  Auth failures show up in the
		// log as `[caller=?] ... status=PermissionDenied` (the
		// context has no derived machine_id yet because auth never
		// got a chance to populate it).  If auth succeeds, the
		// authenticated id is in the context by the time the inner
		// handler returns and logmw reads it for the log line.
		opts = append(opts, grpc.ChainUnaryInterceptor(
			logmw.UnaryInterceptor(),
			auth.UnaryInterceptor(enroller),
		))
		opts = append(opts, grpc.ChainStreamInterceptor(
			logmw.StreamInterceptor(),
			auth.StreamInterceptor(enroller),
		))
	}

	lis, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}

	srv := grpc.NewServer(opts...)
	syncpb.RegisterBrowserVisitSyncServer(srv, server.New(st))

	log.Printf("sync-server listening on %s (insecure=%v)", *listen, *insecure)
	if err := srv.Serve(lis); err != nil {
		log.Fatalf("serve: %v", err)
	}
	_ = os.Stderr
}

func buildTLS(certPath, keyPath, caPath string) (credentials.TransportCredentials, error) {
	cert, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	caPEM, err := os.ReadFile(caPath)
	if err != nil {
		return nil, err
	}
	pool.AppendCertsFromPEM(caPEM)
	return credentials.NewTLS(&tls.Config{
		Certificates: []tls.Certificate{cert},
		ClientCAs:    pool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		// TLS 1.2 floor: gives us interop with stdlib ssl on older
		// macOS LibreSSL while keeping mTLS as the actual auth
		// boundary.  Bump if we ever drop those clients.
		MinVersion: tls.VersionTLS12,
	}), nil
}
