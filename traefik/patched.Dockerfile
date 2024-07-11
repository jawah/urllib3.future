FROM winamd64/golang:1.21-nanoserver as build

WORKDIR /go/src/github.com/mccutchen/go-httpbin

COPY . .

RUN mkdir dist
RUN go build -ldflags="-s" -o dist/go-httpbin ./cmd/go-httpbin

FROM mcr.microsoft.com/windows/server:ltsc2022

WORKDIR /app
COPY --from=build /go/src/github.com/mccutchen/go-httpbin/dist/go-httpbin /app/go-httpbin

EXPOSE 8080
CMD ["/app/go-httpbin"]
