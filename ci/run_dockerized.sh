#!/usr/bin/env sh
docker build -f Dockerfile -t urllib3:main .
docker run -t urllib3:main
