package store

import (
	"os"
	"path/filepath"
	"testing"
)

func writeRaw(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o644)
}

func openTestStore(t *testing.T) *Store {
	t.Helper()
	dir := t.TempDir()
	st, err := Open(filepath.Join(dir, "logs"), filepath.Join(dir, "db", "visits.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func TestLogRootAccessor(t *testing.T) {
	st := openTestStore(t)
	if st.LogRoot() == "" {
		t.Fatal("LogRoot should be non-empty")
	}
}

func TestOpenCreatesDirsAndSchema(t *testing.T) {
	st := openTestStore(t)
	// Schema present: a bare query against each table should succeed.
	for _, tbl := range []string{"visits", "read_events", "skimmed_events", "snapshots"} {
		if _, err := st.DB().Exec("SELECT * FROM " + tbl + " LIMIT 0"); err != nil {
			t.Errorf("table %s missing: %v", tbl, err)
		}
	}
}

func TestAppendLogLinesEmptyReturnsHighWater(t *testing.T) {
	st := openTestStore(t)
	// No lines, nothing on disk yet → date "", offset -1.
	date, off, err := st.AppendLogLines("laptop-a", nil)
	if err != nil {
		t.Fatalf("AppendLogLines empty: %v", err)
	}
	if date != "" || off != -1 {
		t.Fatalf("got (%q,%d), want (\"\",-1)", date, off)
	}
}

func TestAppendLogLinesAppendsAndIsIdempotent(t *testing.T) {
	st := openTestStore(t)
	batch := []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
		{Date: "2026-05-21", LineOffset: 1, RawLine: "b"},
	}
	d, off, err := st.AppendLogLines("laptop-a", batch)
	if err != nil {
		t.Fatalf("append: %v", err)
	}
	if d != "2026-05-21" || off != 1 {
		t.Fatalf("got (%q,%d), want (2026-05-21,1)", d, off)
	}

	// Replaying the same batch is a no-op (dedup against existing length).
	d2, off2, err := st.AppendLogLines("laptop-a", batch)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if d2 != "2026-05-21" || off2 != 1 {
		t.Fatalf("replay got (%q,%d), want (2026-05-21,1)", d2, off2)
	}

	// Only two lines on disk.
	var lines []LogLine
	if err := st.LinesAfter("laptop-a", "", -1, func(l LogLine) bool {
		lines = append(lines, l)
		return true
	}); err != nil {
		t.Fatalf("LinesAfter: %v", err)
	}
	if len(lines) != 2 {
		t.Fatalf("expected 2 lines on disk, got %d", len(lines))
	}
}

func TestAppendLogLinesGapIsError(t *testing.T) {
	st := openTestStore(t)
	_, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 5, RawLine: "x"},
	})
	if err == nil {
		t.Fatal("expected gap error, got nil")
	}
}

func TestAppendLogLinesMultiDate(t *testing.T) {
	st := openTestStore(t)
	d, off, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a"},
		{Date: "2026-05-22", LineOffset: 0, RawLine: "b"},
	})
	if err != nil {
		t.Fatalf("append multi-date: %v", err)
	}
	// Latest group wins for the returned high-water mark.
	if d != "2026-05-22" || off != 0 {
		t.Fatalf("got (%q,%d), want (2026-05-22,0)", d, off)
	}
}

func TestLinesAfterCursorAndMultiDay(t *testing.T) {
	st := openTestStore(t)
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a0"},
		{Date: "2026-05-21", LineOffset: 1, RawLine: "a1"},
	}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-22", LineOffset: 0, RawLine: "b0"},
	}); err != nil {
		t.Fatal(err)
	}

	// Cursor at (2026-05-21, 0): should see a1, b0 — not a0.
	var got []string
	if err := st.LinesAfter("laptop-a", "2026-05-21", 0, func(l LogLine) bool {
		got = append(got, l.RawLine)
		return true
	}); err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != "a1" || got[1] != "b0" {
		t.Fatalf("cursor walk wrong: %v", got)
	}
}

func TestLinesAfterEarlyStop(t *testing.T) {
	st := openTestStore(t)
	if _, _, err := st.AppendLogLines("laptop-a", []LogLine{
		{Date: "2026-05-21", LineOffset: 0, RawLine: "a0"},
		{Date: "2026-05-21", LineOffset: 1, RawLine: "a1"},
	}); err != nil {
		t.Fatal(err)
	}
	count := 0
	if err := st.LinesAfter("laptop-a", "", -1, func(l LogLine) bool {
		count++
		return false // stop after the first
	}); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("early stop: callback ran %d times, want 1", count)
	}
}

func TestLinesAfterMissingMachine(t *testing.T) {
	st := openTestStore(t)
	// Unknown machine dir → no error, no lines.
	called := false
	if err := st.LinesAfter("ghost", "", -1, func(l LogLine) bool {
		called = true
		return true
	}); err != nil {
		t.Fatalf("LinesAfter missing: %v", err)
	}
	if called {
		t.Fatal("callback should not run for missing machine")
	}
}

func TestLinesAfterTrailingPartialLine(t *testing.T) {
	st := openTestStore(t)
	// Write a file with no trailing newline by going around AppendLogLines.
	path, err := st.pathFor("laptop-a", "2026-05-21")
	if err != nil {
		t.Fatal(err)
	}
	if err := writeRaw(path, "line0\nline1-no-newline"); err != nil {
		t.Fatal(err)
	}
	var got []string
	if err := st.LinesAfter("laptop-a", "", -1, func(l LogLine) bool {
		got = append(got, l.RawLine)
		return true
	}); err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[1] != "line1-no-newline" {
		t.Fatalf("partial-line handling wrong: %v", got)
	}
}

func TestEnrolledMachines(t *testing.T) {
	st := openTestStore(t)
	for _, m := range []string{"laptop-b", "laptop-a"} {
		if _, _, err := st.AppendLogLines(m, []LogLine{
			{Date: "2026-05-21", LineOffset: 0, RawLine: "x"},
		}); err != nil {
			t.Fatal(err)
		}
	}
	got, err := st.EnrolledMachines()
	if err != nil {
		t.Fatal(err)
	}
	// Sorted.
	if len(got) != 2 || got[0] != "laptop-a" || got[1] != "laptop-b" {
		t.Fatalf("EnrolledMachines: %v", got)
	}
}

func TestHighWaterEmptyMachine(t *testing.T) {
	st := openTestStore(t)
	d, off, err := st.highWater("nobody")
	if err != nil {
		t.Fatal(err)
	}
	if d != "" || off != -1 {
		t.Fatalf("highWater empty: (%q,%d)", d, off)
	}
}

func TestParseLogFilename(t *testing.T) {
	cases := map[string]bool{
		"browser-visits-2026-05-21.log": true,
		"browser-visits-2026-5-21.log":  false,
		"MANIFEST.tsv":                  false,
		"browser-visits-.log":           false,
	}
	for name, want := range cases {
		_, ok := parseLogFilename(name)
		if ok != want {
			t.Errorf("parseLogFilename(%q)=%v want %v", name, ok, want)
		}
	}
}

func TestGroupByDate(t *testing.T) {
	groups := groupByDate([]LogLine{
		{Date: "d1"}, {Date: "d1"}, {Date: "d2"},
	})
	if len(groups) != 2 || len(groups[0]) != 2 || len(groups[1]) != 1 {
		t.Fatalf("groupByDate wrong: %v", groups)
	}
}

func TestIndexByte(t *testing.T) {
	if indexByte([]byte("abc"), 'b') != 1 {
		t.Fatal("indexByte hit wrong")
	}
	if indexByte([]byte("abc"), 'z') != -1 {
		t.Fatal("indexByte miss should be -1")
	}
}
