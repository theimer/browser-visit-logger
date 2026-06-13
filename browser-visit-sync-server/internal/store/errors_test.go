package store

import (
	"os"
	"path/filepath"
	"testing"
)

// highWater's non-trivial path: an empty AppendLogLines call after data
// exists makes it walk the dir and count the latest day's lines.
func TestHighWaterPopulated(t *testing.T) {
	st := openTestStore(t)
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-20", LineOffset: 0, RawLine: "a"},
		{Date: "2026-05-21", LineOffset: 0, RawLine: "b"},
		{Date: "2026-05-21", LineOffset: 1, RawLine: "c"},
	}); err != nil {
		t.Fatal(err)
	}
	// Empty batch → AppendLogLines delegates to highWater.
	d, off, err := st.AppendLogLines("laptop-a", nil)
	if err != nil {
		t.Fatal(err)
	}
	if d != "2026-05-21" || off != 1 {
		t.Fatalf("highWater populated got (%q,%d) want (2026-05-21,1)", d, off)
	}
}

// Open should fail when the log-root path can't be created because a
// parent component is a regular file.
func TestOpenMkdirLogRootError(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// logRoot under a file → MkdirAll fails.
	_, err := Open(filepath.Join(blocker, "logs"), filepath.Join(dir, "db", "x.db"))
	if err == nil {
		t.Fatal("expected mkdir log root error")
	}
}

// Open should fail when the DB directory can't be created.
func TestOpenMkdirDBDirError(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := Open(filepath.Join(dir, "logs"), filepath.Join(blocker, "db", "x.db"))
	if err == nil {
		t.Fatal("expected mkdir db dir error")
	}
}

// When a machine "directory" is actually a regular file, pathFor's
// MkdirAll fails — surfaced through AppendLogLines.
func TestAppendLogLinesPathForError(t *testing.T) {
	st := openTestStore(t)
	// Create a file where the machine dir should be.
	machineAsFile := filepath.Join(st.logRoot, "laptop-x")
	if err := os.WriteFile(machineAsFile, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := st.AppendLogLines("laptop-x", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
	})
	if err == nil {
		t.Fatal("expected pathFor mkdir error")
	}
}

// countLines should surface a read error when the log "file" is a
// directory (Open succeeds, Read fails with EISDIR).
func TestCountLinesReadError(t *testing.T) {
	st := openTestStore(t)
	// Make the day-log path a directory.
	machineDir := filepath.Join(st.logRoot, "laptop-a")
	if err := os.MkdirAll(machineDir, 0o755); err != nil {
		t.Fatal(err)
	}
	logAsDir := filepath.Join(machineDir, "browser-visits-2026-05-21.log")
	if err := os.MkdirAll(logAsDir, 0o755); err != nil {
		t.Fatal(err)
	}
	_, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
	})
	if err == nil {
		t.Fatal("expected countLines read error")
	}
}

// Closing the DB makes every subsequent Exec fail, exercising the
// error-return branches in the replay paths.
func TestReplayDBErrors(t *testing.T) {
	st := openTestStore(t)
	st.db.Close() // force "database is closed" on the next Exec

	if err := st.ReplayLine(tsv("rid", "ts", "u", "t")); err == nil {
		t.Error("replayBasic should surface DB error")
	}
	if err := st.ReplayLine(tsv("rid", "ts", "u", "t", "of_interest")); err == nil {
		t.Error("5-field replay should surface DB error")
	}
	if err := st.ReplayLine(tsv("rid", "ts", "u", "t", "read", "f")); err == nil {
		t.Error("6-field replay should surface DB error")
	}
}

// setOfInterest / tagWithFilename's own error returns: replayBasic must
// succeed first, then the follow-up statement must fail.  We force that
// by dropping the visits table's columns out from under the UPDATE —
// simplest is to drop the table after the INSERT-able schema check by
// using a DB whose visits table is missing the of_interest column.
func TestTagErrorsAfterVisitInsert(t *testing.T) {
	st := openTestStore(t)
	// Rename the column the follow-up UPDATEs depend on so INSERT into
	// visits still works but the UPDATE statements fail.
	if _, err := st.db.Exec(`ALTER TABLE visits DROP COLUMN of_interest`); err != nil {
		t.Fatalf("prep: %v", err)
	}
	if err := st.ReplayLine(tsv("rid", "ts", "u1", "t", "of_interest")); err == nil {
		t.Error("setOfInterest should fail after column drop")
	}

	// For read/skimmed, drop the counter column.
	if _, err := st.db.Exec(`ALTER TABLE visits DROP COLUMN read`); err != nil {
		t.Fatalf("prep read: %v", err)
	}
	if err := st.ReplayLine(tsv("rid", "ts", "u2", "t", "read", "f")); err == nil {
		t.Error("tagWithFilename UPDATE should fail after counter column drop")
	}
}

// LinesAfter should surface an os.Open error when a day-log file is
// unreadable.
func TestLinesAfterOpenError(t *testing.T) {
	st := openTestStore(t)
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
	}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(st.logRoot, "laptop-a", "browser-visits-2026-05-21.log")
	if err := os.Chmod(path, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(path, 0o644) })
	err := st.LinesAfter("laptop-a", "", -1, func(LogLine) bool { return true })
	if err == nil {
		t.Fatal("expected open error from unreadable log")
	}
}

// EnrolledMachines / highWater should surface a ReadDir / stat error
// when the log root is unreadable.
func TestReadDirErrors(t *testing.T) {
	st := openTestStore(t)
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
	}); err != nil {
		t.Fatal(err)
	}
	machineDir := filepath.Join(st.logRoot, "laptop-a")
	if err := os.Chmod(machineDir, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(machineDir, 0o755) })
	// highWater walks the machine dir → ReadDir fails.
	if _, _, err := st.AppendLogLines("laptop-a", nil); err == nil {
		t.Error("highWater should surface ReadDir error")
	}
}

// EnrolledMachines surfaces a ReadDir error when the log root is
// unreadable.
func TestEnrolledMachinesReadDirError(t *testing.T) {
	st := openTestStore(t)
	if err := os.Chmod(st.logRoot, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.Chmod(st.logRoot, 0o755) })
	if _, err := st.EnrolledMachines(); err == nil {
		t.Fatal("expected ReadDir error")
	}
}

// highWater surfaces a countLines error when the latest day-log path is
// a directory rather than a file.
func TestHighWaterCountLinesError(t *testing.T) {
	st := openTestStore(t)
	machineDir := filepath.Join(st.logRoot, "laptop-a")
	if err := os.MkdirAll(machineDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// A valid-looking day-log name that's actually a directory.
	if err := os.MkdirAll(
		filepath.Join(machineDir, "browser-visits-2026-05-21.log"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, err := st.highWater("laptop-a"); err == nil {
		t.Fatal("expected countLines error from highWater")
	}
}

// LinesAfter: yield returning false on a trailing partial (no final
// newline) line stops iteration via the partial-line branch.
func TestLinesAfterTrailingPartialEarlyStop(t *testing.T) {
	st := openTestStore(t)
	path, err := st.pathFor("laptop-a", "2026-05-21")
	if err != nil {
		t.Fatal(err)
	}
	if err := writeRaw(path, "partial-no-newline"); err != nil {
		t.Fatal(err)
	}
	got := 0
	if err := st.LinesAfter("laptop-a", "", -1, func(LogLine) bool {
		got++
		return false // stop on the trailing partial line
	}); err != nil {
		t.Fatal(err)
	}
	if got != 1 {
		t.Fatalf("expected 1 yielded partial line, got %d", got)
	}
}

// EnrolledMachines skips non-directory entries in the log root.
func TestEnrolledMachinesSkipsFiles(t *testing.T) {
	st := openTestStore(t)
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
	}); err != nil {
		t.Fatal(err)
	}
	// Stray file in the log root.
	if err := os.WriteFile(filepath.Join(st.logRoot, "stray.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := st.EnrolledMachines()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "laptop-a" {
		t.Fatalf("EnrolledMachines should skip files: %v", got)
	}
}
