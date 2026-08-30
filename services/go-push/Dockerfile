# A multi-stage build: a fat stage that compiles, a near-empty stage that ships.
#
# ─── Why this is so different from the Django image ──────────────────────────
#
# Your Django image has to carry a Python interpreter, pip, every wheel in
# requirements.txt (psycopg, Pillow, cryptography — several of which build
# native extensions), and the source tree. It lands somewhere around 150–400MB
# and every one of those packages is a thing that can have a CVE filed against
# it next week.
#
# A Go binary has already absorbed its dependencies at compile time. The
# runtime stage below contains the binary, a CA bundle, and nothing else — no
# shell, no package manager, no libc. That is not a size micro-optimisation:
# it is a much smaller attack surface, and it means "what is running in
# production" is one file you can hash.


# ─── Stage 1: build ──────────────────────────────────────────────────────────
FROM golang:1.23-alpine AS build

WORKDIR /src

# Dependency files first, so Docker can cache the download layer separately
# from the source. Same instinct as copying requirements.txt before your app
# code — except this project has no dependencies, so the layer is nearly empty
# and `go mod download` is a no-op. It stays because the day you add one
# dependency you want the caching already in place.
COPY go.mod ./
RUN go mod download

COPY . .

# Run the tests inside the build. A broken build cannot produce an image.
RUN go vet ./... && go test ./...

# The build flags are the load-bearing part:
#
#   CGO_ENABLED=0  no libc. This is what makes the binary *static* — with cgo
#                  on, Go's DNS resolver and user lookup link against the
#                  system libc and the binary will not run on `scratch`. This
#                  single flag is the difference between a 12MB image and
#                  needing a whole distro underneath it.
#   GOOS/GOARCH    cross-compilation is a flag, not a toolchain. Building a
#                  Linux arm64 binary from a Windows laptop is `GOOS=linux
#                  GOARCH=arm64 go build`, and nothing else.
#   -trimpath      strips local filesystem paths out of the binary, so it does
#                  not embed C:\Users\... and so builds are reproducible.
#   -ldflags       -s -w drop the symbol table and DWARF debug info: roughly a
#                  third off the binary. Skip them if you want usable stack
#                  traces from a core dump.
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/approx-push .


# ─── Stage 2: run ────────────────────────────────────────────────────────────
# `scratch` is the empty image. Literally nothing in it.
FROM scratch

# The one thing a static binary still needs from the outside world: root
# certificates. Without these, every HTTPS call to Expo fails with
# "x509: certificate signed by unknown authority" — and this is *the* classic
# scratch gotcha, because it works perfectly on your laptop where the OS has a
# CA bundle and fails the moment it is containerised.
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Timezone data, if you ever format a local time. Cheap insurance; a static
# binary has no /usr/share/zoneinfo to fall back on.
# (Go can also embed this with a `time/tzdata` import if you prefer.)
COPY --from=build /usr/share/zoneinfo /usr/share/zoneinfo

COPY --from=build /out/approx-push /approx-push

# Run as a non-root UID. There is no /etc/passwd in scratch, so this is a bare
# numeric id rather than a username — which is fine, and is what the Kubernetes
# securityContext expects to match anyway.
USER 65534:65534

EXPOSE 8080

# The exec form (a JSON array) matters: it makes the binary PID 1 directly,
# with no shell in between. A shell as PID 1 does not forward SIGTERM to its
# child, which would silently defeat every line of graceful-shutdown code in
# main.go — the pod would be SIGKILLed with a full queue on every deploy.
ENTRYPOINT ["/approx-push"]
