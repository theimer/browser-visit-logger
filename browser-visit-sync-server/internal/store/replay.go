package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// ReplayLine parses one TSV log line and applies it to the canonical
// DB.  Matches visits_rebuilder.py's logic:
//   - 4 fields:  record_id, timestamp, url, title           → INSERT OR IGNORE visits
//   - 5 fields:  ..., tag (of_interest)                     → set of_interest = 1
//   - 6 fields:  ..., tag (read|skimmed), filename          → increment counter + insert event
//   - 2 fields:  record_id, result                          → ignored (result line)
//
// Lines that don't match any known shape are skipped (caller can log).
func (s *Store) ReplayLine(raw string) error {
	fields := strings.Split(raw, "\t")
	switch len(fields) {
	case 2:
		return nil // result line — no DB effect
	case 4:
		return s.replayBasic(fields[1], fields[2], fields[3])
	case 5:
		if err := s.replayBasic(fields[1], fields[2], fields[3]); err != nil {
			return err
		}
		if fields[4] == "of_interest" {
			return s.setOfInterest(fields[2])
		}
		return nil
	case 6:
		if err := s.replayBasic(fields[1], fields[2], fields[3]); err != nil {
			return err
		}
		return s.tagWithFilename(fields[2], fields[1], fields[4], fields[5])
	default:
		return nil
	}
}

func (s *Store) replayBasic(timestamp, url, title string) error {
	_, err := s.db.Exec(
		`INSERT OR IGNORE INTO visits (url, timestamp, title) VALUES (?, ?, ?)`,
		url, timestamp, title)
	return err
}

func (s *Store) setOfInterest(url string) error {
	_, err := s.db.Exec(`UPDATE visits SET of_interest = '1' WHERE url = ?`, url)
	return err
}

func (s *Store) tagWithFilename(url, timestamp, tag, filename string) error {
	var (
		eventsTable string
		counterCol  string
	)
	switch tag {
	case "read":
		eventsTable = "read_events"
		counterCol = "read"
	case "skimmed":
		eventsTable = "skimmed_events"
		counterCol = "skimmed"
	default:
		return nil
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback()
		}
	}()
	// The event row's primary key is (url, timestamp), so INSERT OR
	// IGNORE deduplicates replays of the same log line.  We only
	// bump the per-URL counter when the event row is actually new,
	// which keeps `read`/`skimmed` consistent with the single-host
	// EventsTable.swift semantics (one counter tick per unique
	// (url, timestamp) event).
	res, err := tx.Exec(
		fmt.Sprintf(`INSERT OR IGNORE INTO %s (url, timestamp, filename) VALUES (?, ?, ?)`, eventsTable),
		url, timestamp, filename)
	if err != nil {
		return err
	}
	inserted, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if inserted == 0 {
		return tx.Commit() // duplicate replay; counter already reflects this event
	}
	if _, err = tx.Exec(
		fmt.Sprintf(`UPDATE visits SET %s = %s + 1 WHERE url = ?`, counterCol, counterCol),
		url); err != nil {
		return err
	}
	return tx.Commit()
}

// _ silences an unused-import warning during refactors.
var _ = sql.ErrNoRows
