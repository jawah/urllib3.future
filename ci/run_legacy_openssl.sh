#!/usr/bin/env sh
docker build -f OpenSSL.Dockerfile -t urllib3:legacy-openssl .
docker run -t --name urllib3-legacy-openssl urllib3:legacy-openssl; exit_code=$?
docker cp urllib3-legacy-openssl:/app/ - | tar -xf - --wildcards --no-anchored '.coverage*' 2>/dev/null && mv app/.coverage* . 2>/dev/null || true
rm -rf app/
docker rm urllib3-legacy-openssl
exit ${exit_code}
