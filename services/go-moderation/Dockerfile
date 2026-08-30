# Multi-stage: a build stage with the toolchain, a runtime stage with a binary.
#
# Identical in shape to services/go-push's, and the comments there explain the
# flags in full. The one difference worth calling out is at the bottom.

FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN go vet ./... && go test ./...

# CGO_ENABLED=0 is what makes the binary static, and therefore what makes the
# `scratch` stage below possible at all.
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/go-moderation .

FROM scratch
# Without these, every HTTPS call to Cloudflare fails with
# "x509: certificate signed by unknown authority" — after working perfectly on
# a laptop, where the OS has a CA bundle. The classic scratch gotcha.
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=build /out/go-moderation /go-moderation
USER 65534:65534
EXPOSE 8081

# Exec form, so the binary is PID 1 and receives SIGTERM directly. A shell in
# between would not forward it, and every line of graceful-shutdown code in
# main.go would be dead — the container would be SIGKILLed with jobs in flight.
#
# Unlike go-push, that is survivable here: an abandoned job is still `running`
# in Postgres, and Django's stale sweep re-queues it. The durability lives in
# the database rather than in this process, which is the main reason this
# workload was the better one to move.
ENTRYPOINT ["/go-moderation"]
