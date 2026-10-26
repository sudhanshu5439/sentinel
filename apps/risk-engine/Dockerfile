# syntax=docker/dockerfile:1
# Stage 1: build a static binary (no CGO) so it can run on a distroless image
FROM golang:1.24 AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/risk-engine .

# Stage 2: distroless static, non-root: no shell, no package manager, tiny attack surface
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/risk-engine /risk-engine
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/risk-engine"]
