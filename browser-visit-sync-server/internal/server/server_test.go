package server

import (
	"context"
	"path/filepath"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	syncpb "github.com/theimer/browser-visit-logger/browser-visit-sync-server/gen/syncpb"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/auth"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/store"
)

func newTestServer(t *testing.T) *Server {
	t.Helper()
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "logs"), filepath.Join(dir, "db", "v.db"))
	if err != nil {
		t.Fatalf("store.Open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return New(st)
}

// authed returns a context that looks like a successful mTLS pass for id.
func authed(id string) context.Context {
	return auth.ContextWithMachineID(context.Background(), id)
}

// ---- PushLogs ----

func TestPushLogsCrossCheckFails(t *testing.T) {
	s := newTestServer(t)
	// No machine id in context → Unauthenticated.
	_, err := s.PushLogs(context.Background(), &syncpb.PushLogsRequest{MachineId: "laptop-a"})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want Unauthenticated, got %v", err)
	}
}

func TestPushLogsEmptyMachineID(t *testing.T) {
	s := newTestServer(t)
	// CrossCheck passes (ctx id "" == claimed ""), then the explicit
	// empty-machine_id guard fires.
	_, err := s.PushLogs(authed(""), &syncpb.PushLogsRequest{MachineId: ""})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("want InvalidArgument, got %v", err)
	}
}

func TestPushLogsMissingDate(t *testing.T) {
	s := newTestServer(t)
	_, err := s.PushLogs(authed("laptop-a"), &syncpb.PushLogsRequest{
		MachineId: "laptop-a",
		Lines:     []*syncpb.LogLine{{LineOffset: 0, RawLine: "x"}}, // no Date
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("want InvalidArgument, got %v", err)
	}
}

func TestPushLogsSuccessAndReplay(t *testing.T) {
	s := newTestServer(t)
	resp, err := s.PushLogs(authed("laptop-a"), &syncpb.PushLogsRequest{
		MachineId: "laptop-a",
		Lines: []*syncpb.LogLine{
			{Date: "2026-05-21", LineOffset: 0,
				RawLine: "rid\t2026-05-21T10:00:00Z\thttps://x\tTitle"},
			{Date: "2026-05-21", LineOffset: 1, RawLine: "rid\tsuccess"},
		},
	})
	if err != nil {
		t.Fatalf("PushLogs: %v", err)
	}
	if resp.GetAcceptedDate() != "2026-05-21" || resp.GetAcceptedLineOffset() != 1 {
		t.Fatalf("resp: %+v", resp)
	}
	// Replay happened: the visit row exists.
	var n int
	if err := s.store.DB().QueryRow(
		"SELECT COUNT(*) FROM visits WHERE url='https://x'").Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("replay didn't insert visit: %d", n)
	}
}

func TestPushLogsAppendError(t *testing.T) {
	s := newTestServer(t)
	// A gap (offset > 0 on an empty file) makes AppendLogLines fail.
	_, err := s.PushLogs(authed("laptop-a"), &syncpb.PushLogsRequest{
		MachineId: "laptop-a",
		Lines:     []*syncpb.LogLine{{Date: "2026-05-21", LineOffset: 9, RawLine: "x"}},
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal (append gap), got %v", err)
	}
}

func TestPushLogsReplayError(t *testing.T) {
	s := newTestServer(t)
	s.store.DB().Close() // replay Exec will fail, but append (file I/O) still works
	_, err := s.PushLogs(authed("laptop-a"), &syncpb.PushLogsRequest{
		MachineId: "laptop-a",
		Lines: []*syncpb.LogLine{
			{Date: "2026-05-21", LineOffset: 0,
				RawLine: "rid\tts\thttps://x\tTitle"},
		},
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal (replay), got %v", err)
	}
}

// ---- PullLogs ----

type fakePullStream struct {
	grpc.ServerStream
	ctx     context.Context
	sent    []*syncpb.LogChunk
	sendN   int
	failN   int  // fail Send on the (failN)th call (1-based); 0 = never
	failAll bool // fail every Send
}

func (f *fakePullStream) Context() context.Context { return f.ctx }
func (f *fakePullStream) Send(c *syncpb.LogChunk) error {
	f.sendN++
	if f.failAll || (f.failN != 0 && f.sendN == f.failN) {
		return status.Error(codes.Unavailable, "send boom")
	}
	// Copy the lines slice — the caller reuses its backing array
	// across batches (batch = batch[:0]).
	cp := make([]*syncpb.LogLine, len(c.Lines))
	copy(cp, c.Lines)
	f.sent = append(f.sent, &syncpb.LogChunk{PeerMachineId: c.PeerMachineId, Lines: cp})
	return nil
}

func TestPullLogsCrossCheckFails(t *testing.T) {
	s := newTestServer(t)
	err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"},
		&fakePullStream{ctx: context.Background()})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want Unauthenticated, got %v", err)
	}
}

func TestPullLogsStreamsPeersExcludingSelf(t *testing.T) {
	s := newTestServer(t)
	// Seed two machines.
	mustPush(t, s, "laptop-a", "2026-05-21", "a-line")
	mustPush(t, s, "laptop-b", "2026-05-21", "b-line")

	stream := &fakePullStream{ctx: authed("laptop-a")}
	err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"}, stream)
	if err != nil {
		t.Fatalf("PullLogs: %v", err)
	}
	// Only laptop-b's lines come back (self excluded).
	if len(stream.sent) != 1 || stream.sent[0].GetPeerMachineId() != "laptop-b" {
		t.Fatalf("unexpected chunks: %+v", stream.sent)
	}
}

func TestPullLogsHonoursCursor(t *testing.T) {
	s := newTestServer(t)
	mustPushOffsets(t, s, "laptop-b", "2026-05-21", []string{"l0", "l1"})

	stream := &fakePullStream{ctx: authed("laptop-a")}
	err := s.PullLogs(&syncpb.PullLogsRequest{
		MachineId: "laptop-a",
		Cursors: []*syncpb.PeerCursor{
			{PeerMachineId: "laptop-b", Date: "2026-05-21", LineOffset: 0},
		},
	}, stream)
	if err != nil {
		t.Fatal(err)
	}
	// Cursor at offset 0 → only l1 streamed.
	if len(stream.sent) != 1 || len(stream.sent[0].Lines) != 1 ||
		stream.sent[0].Lines[0].GetRawLine() != "l1" {
		t.Fatalf("cursor not honoured: %+v", stream.sent)
	}
}

func TestPullLogsBatchedMidScanSend(t *testing.T) {
	s := newTestServer(t)
	// 130 lines for one peer forces a mid-scan flush at the 128 mark
	// (the batched stream.Send inside LinesAfter's yield).
	raws := make([]string, 130)
	for i := range raws {
		raws[i] = "line"
	}
	mustPushOffsets(t, s, "laptop-b", "2026-05-21", raws)

	stream := &fakePullStream{ctx: authed("laptop-a")}
	if err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"}, stream); err != nil {
		t.Fatalf("PullLogs: %v", err)
	}
	// One full batch of 128 + a final flush of 2.
	total := 0
	for _, c := range stream.sent {
		total += len(c.Lines)
	}
	if total != 130 {
		t.Fatalf("expected 130 lines streamed, got %d", total)
	}
}

func TestPullLogsBatchedMidScanSendError(t *testing.T) {
	s := newTestServer(t)
	raws := make([]string, 130)
	for i := range raws {
		raws[i] = "line"
	}
	mustPushOffsets(t, s, "laptop-b", "2026-05-21", raws)

	// Fail every Send so the mid-scan batched Send error (return false)
	// is exercised and the subsequent final-flush Send also fails.
	stream := &fakePullStream{ctx: authed("laptop-a"), failAll: true}
	err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"}, stream)
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("want Unavailable, got %v", err)
	}
}

func TestPullLogsSendErrorFinalFlush(t *testing.T) {
	s := newTestServer(t)
	mustPush(t, s, "laptop-b", "2026-05-21", "b-line")
	stream := &fakePullStream{ctx: authed("laptop-a"), failN: 1}
	err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"}, stream)
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("want Unavailable from final-flush Send, got %v", err)
	}
}

func TestPullLogsScanError(t *testing.T) {
	s := newTestServer(t)
	mustPush(t, s, "laptop-b", "2026-05-21", "b-line")
	// Make laptop-b's log dir unreadable so LinesAfter errors.
	makeUnreadable(t, s, "laptop-b")
	stream := &fakePullStream{ctx: authed("laptop-a")}
	err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"}, stream)
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal (scan), got %v", err)
	}
}

func TestPullLogsEnrolledError(t *testing.T) {
	s := newTestServer(t)
	mustPush(t, s, "laptop-b", "2026-05-21", "b-line")
	makeLogRootUnreadable(t, s)
	stream := &fakePullStream{ctx: authed("laptop-a")}
	err := s.PullLogs(&syncpb.PullLogsRequest{MachineId: "laptop-a"}, stream)
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal (enrolled), got %v", err)
	}
}

// ---- ExportDbSnapshot ----

type fakeSnapStream struct {
	grpc.ServerStream
	ctx     context.Context
	total   int
	failN   int
	sendN   int
}

func (f *fakeSnapStream) Context() context.Context { return f.ctx }
func (f *fakeSnapStream) Send(c *syncpb.DbSnapshotChunk) error {
	f.sendN++
	if f.failN != 0 && f.sendN == f.failN {
		return status.Error(codes.Unavailable, "snap send boom")
	}
	f.total += len(c.GetData())
	return nil
}

func TestExportCrossCheckFails(t *testing.T) {
	s := newTestServer(t)
	err := s.ExportDbSnapshot(&syncpb.ExportDbSnapshotRequest{MachineId: "laptop-a"},
		&fakeSnapStream{ctx: context.Background()})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want Unauthenticated, got %v", err)
	}
}

func TestExportSuccess(t *testing.T) {
	s := newTestServer(t)
	mustPush(t, s, "laptop-a", "2026-05-21",
		"rid\t2026-05-21T10:00:00Z\thttps://x\tTitle")
	stream := &fakeSnapStream{ctx: authed("laptop-a")}
	if err := s.ExportDbSnapshot(
		&syncpb.ExportDbSnapshotRequest{MachineId: "laptop-a"}, stream); err != nil {
		t.Fatalf("Export: %v", err)
	}
	if stream.total == 0 {
		t.Fatal("expected snapshot bytes")
	}
}

func TestExportVacuumError(t *testing.T) {
	s := newTestServer(t)
	s.store.DB().Close() // VACUUM INTO fails on a closed DB
	err := s.ExportDbSnapshot(
		&syncpb.ExportDbSnapshotRequest{MachineId: "laptop-a"},
		&fakeSnapStream{ctx: authed("laptop-a")})
	if status.Code(err) != codes.Internal {
		t.Fatalf("want Internal (vacuum), got %v", err)
	}
}

func TestExportSendError(t *testing.T) {
	s := newTestServer(t)
	mustPush(t, s, "laptop-a", "2026-05-21",
		"rid\t2026-05-21T10:00:00Z\thttps://x\tTitle")
	stream := &fakeSnapStream{ctx: authed("laptop-a"), failN: 1}
	err := s.ExportDbSnapshot(
		&syncpb.ExportDbSnapshotRequest{MachineId: "laptop-a"}, stream)
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("want Unavailable (send), got %v", err)
	}
}

func TestEscapeSinglequotes(t *testing.T) {
	if escapeSinglequotes("a'b") != "a''b" {
		t.Fatal("escape")
	}
}
