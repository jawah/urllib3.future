#!/usr/bin/env sh
docker build -f LibreSSL.Dockerfile -t urllib3:legacy-libressl .
docker run -t urllib3:legacy-libressl
