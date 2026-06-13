// Package store mirrors each laptop's per-day log file on the VM and
// replays incoming lines into the canonical SQLite DB.
//
// On-disk layout:
//
//	<logRoot>/<machine_id>/browser-visits-YYYY-MM-DD.log
//
// PushLogs is idempotent on (machine_id, date, line_offset): the
// store accepts any prefix or contiguous suffix of what's already on
// disk for that file and drops duplicates.  Out-of-order pushes (a
// later offset arriving before an earlier one) are rejected as an
// invariant violation by the caller; BVLSync never produces that
// pattern.
package store

import (
	"database/sql"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"

	_ "modernc.org/sqlite"
)

// Store handles all VM-side persistence: per-machine log files plus
// the canonical SQLite DB.
type Store struct {
	logRoot string
	db      *sql.DB

	mu sync.Mutex // serialises per-file appends
}

func Open(logRoot, dbPath string) (*Store, error) {
	if err := os.MkdirAll(logRoot, 0o755); err != nil {
		return nil, fmt.Errorf("mkdir log root: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(dbPath), 0o755); err != nil {
		return nil, fmt.Errorf("mkdir db dir: %w", err)
	}
	db, err := sql.Open("sqlite", dbPath+"?_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)")
	if err != nil {
		return nil, err
	}
	if err := ensureSchema(db); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{logRoot: logRoot, db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

// AppendLogLines writes the supplied lines to the per-machine, per-day
// file iff they extend the file (line_offset == current line count).
// Lines that fall strictly before the current count are silently
// dropped as already-recorded.  A gap (offset > count) is an error.
//
// Returns the new high-water (date, line_offset) for that machine.
func (s *Store) AppendLogLines(machineID string, lines []LogLine) (string, int64, error) {
	if len(lines) == 0 {
		date, off, err := s.highWater(machineID)
		return date, off, err
	}
	// Group by date so we can write whole files in one open.
	sort.Slice(lines, func(i, j int) bool {
		if lines[i].Date != lines[j].Date {
			return lines[i].Date < lines[j].Date
		}
		return lines[i].LineOffset < lines[j].LineOffset
	})

	s.mu.Lock()
	defer s.mu.Unlock()

	var (
		latestDate   string
		latestOffset int64
	)
	for _, group := range groupByDate(lines) {
		path, err := s.pathFor(machineID, group[0].Date)
		if err != nil {
			return "", 0, err
		}
		curCount, err := countLines(path)
		if err != nil {
			return "", 0, err
		}
		toWrite := []LogLine{}
		for _, l := range group {
			switch {
			case l.LineOffset < curCount:
				continue // already on disk
			case l.LineOffset == curCount:
				toWrite = append(toWrite, l)
				curCount++
			default:
				return "", 0, fmt.Errorf("gap: %s/%s expected offset %d, got %d", machineID, l.Date, curCount, l.LineOffset)
			}
		}
		if len(toWrite) > 0 {
			if err := appendFile(path, toWrite); err != nil {
				return "", 0, err
			}
		}
		latestDate = group[0].Date
		latestOffset = curCount - 1
	}
	return latestDate, latestOffset, nil
}

// LinesAfter streams lines for one machine that come strictly after
// the supplied cursor, ordered by (date asc, line_offset asc).
//
// The yield func returns false to stop iteration early.
func (s *Store) LinesAfter(machineID, sinceDate string, sinceOffset int64, yield func(LogLine) bool) error {
	dir := filepath.Join(s.logRoot, machineID)
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	var dates []string
	for _, e := range entries {
		if d, ok := parseLogFilename(e.Name()); ok {
			if d >= sinceDate {
				dates = append(dates, d)
			}
		}
	}
	sort.Strings(dates)
	for _, d := range dates {
		path := filepath.Join(dir, fmt.Sprintf("browser-visits-%s.log", d))
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		err = func() error {
			defer f.Close()
			var offset int64
			buf := make([]byte, 0, 4096)
			scratch := make([]byte, 4096)
			for {
				n, err := f.Read(scratch)
				if n > 0 {
					buf = append(buf, scratch[:n]...)
					for {
						i := indexByte(buf, '\n')
						if i < 0 {
							break
						}
						line := string(buf[:i])
						buf = buf[i+1:]
						if d > sinceDate || offset > sinceOffset {
							if !yield(LogLine{Date: d, LineOffset: offset, RawLine: line}) {
								return io.EOF
							}
						}
						offset++
					}
				}
				if err == io.EOF {
					if len(buf) > 0 {
						// Trailing partial line (no final newline) — emit if past cursor.
						if d > sinceDate || offset > sinceOffset {
							if !yield(LogLine{Date: d, LineOffset: offset, RawLine: string(buf)}) {
								return io.EOF
							}
						}
					}
					return nil
				}
				if err != nil {
					return err
				}
			}
		}()
		if err != nil && !errors.Is(err, io.EOF) {
			return err
		}
	}
	return nil
}

// EnrolledMachines returns every machine_id the VM has ever seen a
// PushLogs from (derived from the directory listing under logRoot).
// Used by PullLogs to decide whose logs to scan when the caller's
// cursor list is missing a peer.
func (s *Store) EnrolledMachines() ([]string, error) {
	entries, err := os.ReadDir(s.logRoot)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			out = append(out, e.Name())
		}
	}
	sort.Strings(out)
	return out, nil
}

// DB returns the underlying sqlite handle so server-level code can run
// VACUUM INTO etc.  Callers must not close it.
func (s *Store) DB() *sql.DB { return s.db }

// LogRoot returns the directory under which per-machine log mirrors
// live.  Exposed so callers (and tests) can locate the on-disk files.
func (s *Store) LogRoot() string { return s.logRoot }

// ---------------------------------------------------------------- helpers

type LogLine struct {
	Date       string
	LineOffset int64
	RawLine    string
}

func groupByDate(lines []LogLine) [][]LogLine {
	var groups [][]LogLine
	start := 0
	for i := 1; i <= len(lines); i++ {
		if i == len(lines) || lines[i].Date != lines[start].Date {
			groups = append(groups, lines[start:i])
			start = i
		}
	}
	return groups
}

func (s *Store) pathFor(machineID, date string) (string, error) {
	dir := filepath.Join(s.logRoot, machineID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return filepath.Join(dir, fmt.Sprintf("browser-visits-%s.log", date)), nil
}

func countLines(path string) (int64, error) {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return 0, nil
		}
		return 0, err
	}
	defer f.Close()
	var n int64
	buf := make([]byte, 64*1024)
	for {
		c, err := f.Read(buf)
		for i := 0; i < c; i++ {
			if buf[i] == '\n' {
				n++
			}
		}
		if err == io.EOF {
			return n, nil
		}
		if err != nil {
			return 0, err
		}
	}
}

func appendFile(path string, lines []LogLine) error {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	var b strings.Builder
	for _, l := range lines {
		b.WriteString(l.RawLine)
		b.WriteByte('\n')
	}
	_, err = f.WriteString(b.String())
	return err
}

func (s *Store) highWater(machineID string) (string, int64, error) {
	dir := filepath.Join(s.logRoot, machineID)
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "", -1, nil
		}
		return "", 0, err
	}
	var dates []string
	for _, e := range entries {
		if d, ok := parseLogFilename(e.Name()); ok {
			dates = append(dates, d)
		}
	}
	if len(dates) == 0 {
		return "", -1, nil
	}
	sort.Strings(dates)
	latest := dates[len(dates)-1]
	count, err := countLines(filepath.Join(dir, fmt.Sprintf("browser-visits-%s.log", latest)))
	if err != nil {
		return "", 0, err
	}
	return latest, count - 1, nil
}

var logFilenameRE = regexp.MustCompile(`^browser-visits-(\d{4}-\d{2}-\d{2})\.log$`)

func parseLogFilename(name string) (string, bool) {
	m := logFilenameRE.FindStringSubmatch(name)
	if m == nil {
		return "", false
	}
	return m[1], true
}

func indexByte(b []byte, c byte) int {
	for i := 0; i < len(b); i++ {
		if b[i] == c {
			return i
		}
	}
	return -1
}

func ensureSchema(db *sql.DB) error {
	// Mirrors browser-visit-logger/schema.sql verbatim. Kept in lockstep
	// by hand for now; a follow-up will load the .sql file at runtime.
	const ddl = `
CREATE TABLE IF NOT EXISTS visits (
    url         TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    of_interest TEXT,
    read        INTEGER NOT NULL DEFAULT 0,
    skimmed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_visits_timestamp ON visits(timestamp);
CREATE TABLE IF NOT EXISTS read_events (
    url       TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    filename  TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (url, timestamp)
);
CREATE TABLE IF NOT EXISTS skimmed_events (
    url       TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    filename  TEXT NOT NULL DEFAULT '',
    directory TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (url, timestamp)
);
CREATE TABLE IF NOT EXISTS snapshots (
    date   TEXT PRIMARY KEY,
    sealed INTEGER NOT NULL DEFAULT 0
);
`
	_, err := db.Exec(ddl)
	return err
}
