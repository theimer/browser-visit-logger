// Package logmw provides a tiny gRPC logging middleware: one line per
// RPC, written to the standard logger.  Format:
//
//	[caller=<authed_machine_id>] <method> status=<code> dur=<duration> <details>
//
// `<details>` is request-specific (e.g. `lines=N` for PushLogs) when
// we can extract something useful without serialising the whole
// proto.  Falls back to empty when nothing notable.
package logmw

import (
	"context"
	"log"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/status"

	syncpb "github.com/theimer/browser-visit-logger/browser-visit-sync-server/gen/syncpb"
)

// callerSlot is a mutable container the auth interceptor writes into
// once it has derived the machine_id from the client cert.  We need
// this indirection because grpc.ChainUnaryInterceptor invokes inner
// interceptors with a *child* context that the outer interceptor
// can't observe — i.e. plain context.WithValue done by auth wouldn't
// surface back to logmw.  A pointer-to-struct in the (immutable)
// context survives that boundary.
type callerSlot struct{ id string }

type slotKey struct{}

// SetCaller records the cert-derived machine_id so logmw can include
// it in the log line.  Called by the auth interceptor.
func SetCaller(ctx context.Context, id string) {
	if s, ok := ctx.Value(slotKey{}).(*callerSlot); ok {
		s.id = id
	}
}

// UnaryInterceptor logs one line per unary RPC.
func UnaryInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		slot := &callerSlot{}
		ctx = context.WithValue(ctx, slotKey{}, slot)
		start := time.Now()
		resp, err := handler(ctx, req)
		log.Printf("%s%s %s dur=%s%s",
			callerPrefix(slot),
			info.FullMethod,
			statusCode(err),
			time.Since(start).Round(time.Microsecond),
			unaryDetails(req, resp))
		return resp, err
	}
}

// StreamInterceptor logs one line per streaming RPC (server- or
// client-streamed).  Request payload isn't available here without
// wrapping ServerStream.RecvMsg, so for streams we just report
// method + auth + status + duration.
func StreamInterceptor() grpc.StreamServerInterceptor {
	return func(srv interface{}, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		slot := &callerSlot{}
		wrapped := &slotInjectingStream{ServerStream: ss,
			ctx: context.WithValue(ss.Context(), slotKey{}, slot)}
		start := time.Now()
		err := handler(srv, wrapped)
		log.Printf("%s%s %s dur=%s (stream)",
			callerPrefix(slot),
			info.FullMethod,
			statusCode(err),
			time.Since(start).Round(time.Microsecond))
		return err
	}
}

// slotInjectingStream lets the chained inner interceptor see our
// slot-augmented context via ServerStream.Context().
type slotInjectingStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s *slotInjectingStream) Context() context.Context { return s.ctx }

func callerPrefix(slot *callerSlot) string {
	if slot.id == "" {
		return "[caller=?] "
	}
	return "[caller=" + slot.id + "] "
}

func statusCode(err error) string {
	if err == nil {
		return "status=OK"
	}
	return "status=" + status.Code(err).String()
}

// unaryDetails extracts a short, structured tail for unary RPCs we
// know about.  Keep this small: noisy logs are worse than thin ones.
func unaryDetails(req, resp interface{}) string {
	switch r := req.(type) {
	case *syncpb.PushLogsRequest:
		dates := uniqueDates(r.GetLines())
		out := " lines=" + itoa(len(r.GetLines()))
		if len(dates) > 0 {
			out += " dates=" + commaJoin(dates)
		}
		if pr, ok := resp.(*syncpb.PushLogsResponse); ok && pr != nil {
			out += " accepted_to=" + pr.GetAcceptedDate() + ":" + itoa64(pr.GetAcceptedLineOffset())
		}
		return out
	}
	return ""
}

// --- tiny formatting helpers; pulled in to avoid bringing fmt.Sprintf
// into a hot path for what's essentially a few integers.

func itoa(n int) string   { return itoa64(int64(n)) }

func itoa64(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

func uniqueDates(lines []*syncpb.LogLine) []string {
	seen := map[string]struct{}{}
	var out []string
	for _, l := range lines {
		d := l.GetDate()
		if _, ok := seen[d]; ok {
			continue
		}
		seen[d] = struct{}{}
		out = append(out, d)
	}
	return out
}

func commaJoin(xs []string) string {
	if len(xs) == 0 {
		return ""
	}
	out := xs[0]
	for _, x := range xs[1:] {
		out += "," + x
	}
	return out
}
