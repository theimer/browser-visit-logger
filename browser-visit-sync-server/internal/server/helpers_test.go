package server

import (
	"os"
	"path/filepath"
	"testing"

	syncpb "github.com/theimer/browser-visit-logger/browser-visit-sync-server/gen/syncpb"
)

// mustPush appends a single raw line for machine/date at offset 0.
func mustPush(t *testing.T, s *Server, machine, date, raw string) {
	t.Helper()
	mustPushOffsets(t, s, machine, date, []string{raw})
}

// mustPushOffsets appends raws at offsets 0..n-1 for machine/date.
func mustPushOffsets(t *testing.T, s *Server, machine, date string, raws []string) {
	t.Helper()
	lines := make([]*syncpb.LogLine, len(raws))
	for i, r := range raws {
		lines[i] = &syncpb.LogLine{Date: date, LineOffset: int64(i), RawLine: r}
	}
	if _, err := s.PushLogs(authed(machine), &syncpb.PushLogsRequest{
		MachineId: machine, Lines: lines,
	}); err != nil {
		t.Fatalf("seed push for %s: %v", machine, err)
	}
}

func makeUnreadable(t *testing.T, s *Server, machine string) {
	t.Helper()
	dir := filepath.Join(s.store.LogRoot(), machine)
	if err := os.Chmod(dir, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(dir, 0o755) })
}

func makeLogRootUnreadable(t *testing.T, s *Server) {
	t.Helper()
	// Restore child perms first on cleanup so TempDir removal works.
	root := s.store.LogRoot()
	if err := os.Chmod(root, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(root, 0o755) })
}
