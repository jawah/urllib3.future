#!/usr/bin/env sh
docker build -f OpenSSL.Dockerfile -t urllib3:legacy-openssl .
docker run -t urllib3:legacy-openssl
