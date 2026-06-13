# Production server image.
#
# The protoc-generated bindings (gen/syncpb/) are intentionally not
# committed.  The build stage installs protoc + the Go plugins and
# runs `make proto` before `go build`, so the same source tree
# produces the same artifact regardless of whether bindings happen
# to be present on the host.
FROM golang:1.22 AS build

RUN apt-get update \
 && apt-get install -y --no-install-recommends protobuf-compiler ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Install codegen plugins into a fixed path so they're on PATH.
ENV GOBIN=/usr/local/bin
RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.1 \
 && go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.4.0

COPY . .
# Generate proto bindings, then resolve transitive deps once both the
# hand-written and the generated source files are present.  go.sum is
# intentionally not committed; the lock comes from go.mod's `require`
# block + go mod tidy.
RUN make proto
RUN go mod tidy
RUN CGO_ENABLED=0 go build -o /out/sync-server ./cmd/sync-server

FROM gcr.io/distroless/static:nonroot
COPY --from=build /out/sync-server /usr/local/bin/sync-server
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/sync-server"]
