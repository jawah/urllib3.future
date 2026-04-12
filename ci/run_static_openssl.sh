#!/usr/bin/env sh
docker build -f StaticOpenSSL.Dockerfile -t urllib3:static-openssl .
docker run -t --name urllib3-static-openssl urllib3:static-openssl; exit_code=$?
docker cp urllib3-static-openssl:/app/ - | tar -xf - --wildcards --no-anchored '.coverage*' 2>/dev/null && mv app/.coverage* . 2>/dev/null || true
rm -rf app/
# Rename bare .coverage to .coverage.static-openssl so it matches the .coverage.* artifact glob
[ -f .coverage ] && mv .coverage .coverage.static-openssl
docker rm urllib3-static-openssl
exit ${exit_code}
