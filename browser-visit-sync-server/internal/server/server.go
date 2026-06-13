// Package server wires the gRPC service to the underlying store.
package server

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	syncpb "github.com/theimer/browser-visit-logger/browser-visit-sync-server/gen/syncpb"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/auth"
	"github.com/theimer/browser-visit-logger/browser-visit-sync-server/internal/store"
)

const snapshotChunkSize = 256 * 1024

type Server struct {
	syncpb.UnimplementedBrowserVisitSyncServer
	store *store.Store
}

func New(s *store.Store) *Server { return &Server{store: s} }

func (s *Server) PushLogs(ctx context.Context, req *syncpb.PushLogsRequest) (*syncpb.PushLogsResponse, error) {
	if err := auth.CrossCheck(ctx, req.GetMachineId()); err != nil {
		return nil, err
	}
	if req.GetMachineId() == "" {
		return nil, status.Error(codes.InvalidArgument, "machine_id required")
	}
	lines := make([]store.LogLine, 0, len(req.GetLines()))
	for _, l := range req.GetLines() {
		if l.GetDate() == "" {
			return nil, status.Error(codes.InvalidArgument, "log line missing date")
		}
		lines = append(lines, store.LogLine{
			Date: l.GetDate(), LineOffset: l.GetLineOffset(), RawLine: l.GetRawLine(),
		})
	}
	latestDate, latestOff, err := s.store.AppendLogLines(req.GetMachineId(), lines)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "append: %v", err)
	}
	for _, l := range lines {
		if err := s.store.ReplayLine(l.RawLine); err != nil {
			return nil, status.Errorf(codes.Internal, "replay: %v", err)
		}
	}
	return &syncpb.PushLogsResponse{
		AcceptedDate:       latestDate,
		AcceptedLineOffset: latestOff,
	}, nil
}

func (s *Server) PullLogs(req *syncpb.PullLogsRequest, stream syncpb.BrowserVisitSync_PullLogsServer) error {
	if err := auth.CrossCheck(stream.Context(), req.GetMachineId()); err != nil {
		return err
	}
	cursors := map[string]*syncpb.PeerCursor{}
	for _, c := range req.GetCursors() {
		cursors[c.GetPeerMachineId()] = c
	}
	enrolled, err := s.store.EnrolledMachines()
	if err != nil {
		return status.Errorf(codes.Internal, "list machines: %v", err)
	}
	for _, peer := range enrolled {
		if peer == req.GetMachineId() {
			continue // don't echo caller's own records
		}
		cursor := cursors[peer]
		sinceDate := ""
		var sinceOff int64 = -1
		if cursor != nil {
			sinceDate = cursor.GetDate()
			sinceOff = cursor.GetLineOffset()
		}
		batch := make([]*syncpb.LogLine, 0, 128)
		err := s.store.LinesAfter(peer, sinceDate, sinceOff, func(l store.LogLine) bool {
			batch = append(batch, &syncpb.LogLine{
				Date: l.Date, LineOffset: l.LineOffset, RawLine: l.RawLine,
			})
			if len(batch) >= 128 {
				if err := stream.Send(&syncpb.LogChunk{PeerMachineId: peer, Lines: batch}); err != nil {
					return false
				}
				batch = batch[:0]
			}
			return true
		})
		if err != nil {
			return status.Errorf(codes.Internal, "scan %s: %v", peer, err)
		}
		if len(batch) > 0 {
			if err := stream.Send(&syncpb.LogChunk{PeerMachineId: peer, Lines: batch}); err != nil {
				return err
			}
		}
	}
	return nil
}

func (s *Server) ExportDbSnapshot(req *syncpb.ExportDbSnapshotRequest, stream syncpb.BrowserVisitSync_ExportDbSnapshotServer) error {
	if err := auth.CrossCheck(stream.Context(), req.GetMachineId()); err != nil {
		return err
	}
	dir, err := os.MkdirTemp("", "bvl-snap-")
	if err != nil {
		return status.Errorf(codes.Internal, "mkdir temp: %v", err)
	}
	defer os.RemoveAll(dir)
	tmpPath := filepath.Join(dir, "snapshot.db")
	// VACUUM INTO produces a transactionally-consistent copy without
	// blocking writers, and emits a deterministic, defragmented file
	// suitable for byte-stream transfer.
	if _, err := s.store.DB().Exec(fmt.Sprintf(`VACUUM INTO '%s'`, escapeSinglequotes(tmpPath))); err != nil {
		return status.Errorf(codes.Internal, "vacuum into: %v", err)
	}
	f, err := os.Open(tmpPath)
	if err != nil {
		return status.Errorf(codes.Internal, "open snapshot: %v", err)
	}
	defer f.Close()
	buf := make([]byte, snapshotChunkSize)
	for {
		n, err := f.Read(buf)
		if n > 0 {
			if sendErr := stream.Send(&syncpb.DbSnapshotChunk{Data: buf[:n]}); sendErr != nil {
				return sendErr
			}
		}
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return status.Errorf(codes.Internal, "read snapshot: %v", err)
		}
	}
}

func escapeSinglequotes(s string) string { return strings.ReplaceAll(s, "'", "''") }
