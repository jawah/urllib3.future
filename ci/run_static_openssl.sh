#!/usr/bin/env sh
docker build -f StaticOpenSSL.Dockerfile -t urllib3:static-openssl .
docker run -t urllib3:static-openssl
