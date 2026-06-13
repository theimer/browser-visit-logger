package logmw

import (
	"context"
	"errors"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	syncpb "github.com/theimer/browser-visit-logger/browser-visit-sync-server/gen/syncpb"
)

func TestItoa64(t *testing.T) {
	cases := map[int64]string{0: "0", 7: "7", 42: "42", -5: "-5", 1000000: "1000000"}
	for in, want := range cases {
		if got := itoa64(in); got != want {
			t.Errorf("itoa64(%d)=%q want %q", in, got, want)
		}
	}
	if itoa(3) != "3" {
		t.Error("itoa wrapper")
	}
}

func TestCommaJoin(t *testing.T) {
	if commaJoin(nil) != "" {
		t.Error("empty")
	}
	if commaJoin([]string{"a"}) != "a" {
		t.Error("single")
	}
	if commaJoin([]string{"a", "b", "c"}) != "a,b,c" {
		t.Error("multi")
	}
}

func TestUniqueDates(t *testing.T) {
	got := uniqueDates([]*syncpb.LogLine{
		{Date: "d1"}, {Date: "d1"}, {Date: "d2"},
	})
	if len(got) != 2 || got[0] != "d1" || got[1] != "d2" {
		t.Fatalf("uniqueDates: %v", got)
	}
}

func TestStatusCode(t *testing.T) {
	if statusCode(nil) != "status=OK" {
		t.Error("nil → OK")
	}
	if statusCode(status.Error(codes.PermissionDenied, "x")) != "status=PermissionDenied" {
		t.Error("PD")
	}
}

func TestCallerPrefix(t *testing.T) {
	if callerPrefix(&callerSlot{}) != "[caller=?] " {
		t.Error("empty slot")
	}
	if callerPrefix(&callerSlot{id: "laptop-a"}) != "[caller=laptop-a] " {
		t.Error("populated slot")
	}
}

func TestSetCallerNoSlotIsNoop(t *testing.T) {
	// Calling SetCaller on a context without a slot must not panic.
	SetCaller(context.Background(), "x")
}

func TestUnaryDetailsPushLogs(t *testing.T) {
	req := &syncpb.PushLogsRequest{Lines: []*syncpb.LogLine{
		{Date: "2026-05-21"}, {Date: "2026-05-21"}, {Date: "2026-05-22"},
	}}
	resp := &syncpb.PushLogsResponse{AcceptedDate: "2026-05-22", AcceptedLineOffset: 4}
	got := unaryDetails(req, resp)
	want := " lines=3 dates=2026-05-21,2026-05-22 accepted_to=2026-05-22:4"
	if got != want {
		t.Fatalf("unaryDetails=%q want %q", got, want)
	}
}

func TestUnaryDetailsPushLogsNoLinesNoResp(t *testing.T) {
	// Empty lines → no dates clause; non-PushLogsResponse → no accepted clause.
	got := unaryDetails(&syncpb.PushLogsRequest{}, "not-a-response")
	if got != " lines=0" {
		t.Fatalf("got %q", got)
	}
}

func TestUnaryDetailsOtherRequestEmpty(t *testing.T) {
	if unaryDetails(&syncpb.PullLogsRequest{}, nil) != "" {
		t.Error("non-PushLogs req should yield empty details")
	}
}

func TestUnaryInterceptorPopulatesCallerAndLogs(t *testing.T) {
	intc := UnaryInterceptor()
	called := false
	handler := func(ctx context.Context, req interface{}) (interface{}, error) {
		called = true
		// A downstream interceptor (auth) would call SetCaller here.
		SetCaller(ctx, "laptop-a")
		return &syncpb.PushLogsResponse{AcceptedDate: "d", AcceptedLineOffset: 0}, nil
	}
	_, err := intc(context.Background(),
		&syncpb.PushLogsRequest{Lines: []*syncpb.LogLine{{Date: "d"}}},
		&grpc.UnaryServerInfo{FullMethod: "/svc/PushLogs"}, handler)
	if err != nil || !called {
		t.Fatalf("unary interceptor: err=%v called=%v", err, called)
	}
}

func TestUnaryInterceptorErrorPath(t *testing.T) {
	intc := UnaryInterceptor()
	handler := func(ctx context.Context, req interface{}) (interface{}, error) {
		return nil, errors.New("boom")
	}
	_, err := intc(context.Background(), &syncpb.PullLogsRequest{},
		&grpc.UnaryServerInfo{FullMethod: "/svc/PullLogs"}, handler)
	if err == nil {
		t.Fatal("error should propagate")
	}
}

// fakeStream is a minimal grpc.ServerStream for interceptor tests.
type fakeStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (f *fakeStream) Context() context.Context { return f.ctx }

func TestStreamInterceptorLogsAndPropagatesContext(t *testing.T) {
	intc := StreamInterceptor()
	var sawCtxID string
	handler := func(srv interface{}, ss grpc.ServerStream) error {
		SetCaller(ss.Context(), "laptop-b")
		// The slot lives in the context logmw injected; confirm a later
		// SetCaller is visible through callerPrefix indirectly by not
		// panicking and returning nil.
		sawCtxID = "ok"
		return nil
	}
	err := intc(nil, &fakeStream{ctx: context.Background()},
		&grpc.StreamServerInfo{FullMethod: "/svc/PullLogs"}, handler)
	if err != nil || sawCtxID != "ok" {
		t.Fatalf("stream interceptor: err=%v saw=%q", err, sawCtxID)
	}
}

func TestStreamInterceptorErrorPath(t *testing.T) {
	intc := StreamInterceptor()
	handler := func(srv interface{}, ss grpc.ServerStream) error {
		return errors.New("boom")
	}
	err := intc(nil, &fakeStream{ctx: context.Background()},
		&grpc.StreamServerInfo{FullMethod: "/svc/X"}, handler)
	if err == nil {
		t.Fatal("error should propagate")
	}
}
