# ecomm-pipeline — automated print-on-demand pipeline (Go).
#
# Multi-stage: build a static, CGO-free binary, then ship it on a tiny
# distroless base. Templates are embedded in the binary (go:embed), so the
# runtime image is just the binary + a /data volume for state and artwork.
FROM golang:1.23 AS build
WORKDIR /src

# Deps first so source edits don't re-download modules.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" \
    -o /out/ecomm-pipeline ./cmd/ecomm-pipeline

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/ecomm-pipeline /ecomm-pipeline
EXPOSE 8810
VOLUME ["/data"]
ENTRYPOINT ["/ecomm-pipeline"]
