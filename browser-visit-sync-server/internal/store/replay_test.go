package store

import (
	"strings"
	"testing"
)

func tsv(fields ...string) string { return strings.Join(fields, "\t") }

func TestReplayResultLineIsNoOp(t *testing.T) {
	st := openTestStore(t)
	if err := st.ReplayLine("rid\tsuccess"); err != nil {
		t.Fatal(err)
	}
	if n := countRows(t, st, "visits"); n != 0 {
		t.Fatalf("result line touched visits: %d", n)
	}
}

func TestReplayTooFewFieldsIgnored(t *testing.T) {
	st := openTestStore(t)
	if err := st.ReplayLine("only-one-field-but-not-two-tabs"); err != nil {
		t.Fatal(err)
	}
	if n := countRows(t, st, "visits"); n != 0 {
		t.Fatalf("malformed line touched visits: %d", n)
	}
}

func TestReplayBasicVisit(t *testing.T) {
	st := openTestStore(t)
	if err := st.ReplayLine(tsv("rid", "2026-05-21T10:00:00Z", "https://x", "Title")); err != nil {
		t.Fatal(err)
	}
	var ts, title string
	row := st.DB().QueryRow("SELECT timestamp, title FROM visits WHERE url=?", "https://x")
	if err := row.Scan(&ts, &title); err != nil {
		t.Fatal(err)
	}
	if ts != "2026-05-21T10:00:00Z" || title != "Title" {
		t.Fatalf("visit row wrong: %q %q", ts, title)
	}
}

func TestReplayOfInterest(t *testing.T) {
	st := openTestStore(t)
	line := tsv("rid", "2026-05-21T10:00:00Z", "https://x", "Title", "of_interest")
	if err := st.ReplayLine(line); err != nil {
		t.Fatal(err)
	}
	var oi string
	if err := st.DB().QueryRow("SELECT of_interest FROM visits WHERE url=?", "https://x").Scan(&oi); err != nil {
		t.Fatal(err)
	}
	if oi != "1" {
		t.Fatalf("of_interest=%q want 1", oi)
	}
}

func TestReplayFiveFieldsUnknownTagJustInsertsVisit(t *testing.T) {
	st := openTestStore(t)
	// 5 fields but tag != of_interest → visit inserted, no flag set.
	line := tsv("rid", "2026-05-21T10:00:00Z", "https://x", "Title", "weird")
	if err := st.ReplayLine(line); err != nil {
		t.Fatal(err)
	}
	var oi *string
	if err := st.DB().QueryRow("SELECT of_interest FROM visits WHERE url=?", "https://x").Scan(&oi); err != nil {
		t.Fatal(err)
	}
	if oi != nil {
		t.Fatalf("of_interest should be NULL, got %v", *oi)
	}
}

func TestReplayReadIncrementsCounterOncePerEvent(t *testing.T) {
	st := openTestStore(t)
	line := tsv("rid", "2026-05-21T10:00:00Z", "https://x", "Title", "read", "a.mhtml")
	if err := st.ReplayLine(line); err != nil {
		t.Fatal(err)
	}
	// Replay identical line → event dedups, counter stays at 1.
	if err := st.ReplayLine(line); err != nil {
		t.Fatal(err)
	}
	var readCount int
	if err := st.DB().QueryRow("SELECT read FROM visits WHERE url=?", "https://x").Scan(&readCount); err != nil {
		t.Fatal(err)
	}
	if readCount != 1 {
		t.Fatalf("read counter=%d want 1 (dedup)", readCount)
	}
	// A second event with a distinct timestamp bumps to 2.
	line2 := tsv("rid", "2026-05-21T10:05:00Z", "https://x", "Title", "read", "b.mhtml")
	if err := st.ReplayLine(line2); err != nil {
		t.Fatal(err)
	}
	if err := st.DB().QueryRow("SELECT read FROM visits WHERE url=?", "https://x").Scan(&readCount); err != nil {
		t.Fatal(err)
	}
	if readCount != 2 {
		t.Fatalf("read counter=%d want 2", readCount)
	}
}

func TestReplaySkimmed(t *testing.T) {
	st := openTestStore(t)
	line := tsv("rid", "2026-05-21T10:00:00Z", "https://x", "Title", "skimmed", "a.mhtml")
	if err := st.ReplayLine(line); err != nil {
		t.Fatal(err)
	}
	var skimmed int
	if err := st.DB().QueryRow("SELECT skimmed FROM visits WHERE url=?", "https://x").Scan(&skimmed); err != nil {
		t.Fatal(err)
	}
	if skimmed != 1 {
		t.Fatalf("skimmed=%d want 1", skimmed)
	}
}

func TestReplaySixFieldsUnknownTagNoop(t *testing.T) {
	st := openTestStore(t)
	// 6 fields but tag is neither read nor skimmed → tagWithFilename
	// returns early after the visit insert.
	line := tsv("rid", "2026-05-21T10:00:00Z", "https://x", "Title", "bogus", "f.mhtml")
	if err := st.ReplayLine(line); err != nil {
		t.Fatal(err)
	}
	if n := countRows(t, st, "read_events"); n != 0 {
		t.Fatalf("unknown 6-field tag created read_events: %d", n)
	}
	if n := countRows(t, st, "visits"); n != 1 {
		t.Fatalf("visit row missing: %d", n)
	}
}

func countRows(t *testing.T, st *Store, table string) int {
	t.Helper()
	var n int
	if err := st.DB().QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
