#!/usr/bin/env sh
docker build -f LibreSSL.Dockerfile -t urllib3:legacy-libressl .
docker run -t --name urllib3-legacy-libressl urllib3:legacy-libressl; exit_code=$?
docker cp urllib3-legacy-libressl:/app/ - | tar -xf - --wildcards --no-anchored '.coverage*' 2>/dev/null && mv app/.coverage* . 2>/dev/null || true
rm -rf app/
docker rm urllib3-legacy-libressl
exit ${exit_code}
