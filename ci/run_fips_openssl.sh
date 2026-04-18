#!/usr/bin/env sh
docker build -f FIPSOpenSSL.Dockerfile -t urllib3:fips-openssl .
docker run -t --name urllib3-fips-openssl urllib3:fips-openssl; exit_code=$?
docker cp urllib3-fips-openssl:/app/ - | tar -xf - --wildcards --no-anchored '.coverage*' 2>/dev/null && mv app/.coverage* . 2>/dev/null || true
rm -rf app/
docker rm urllib3-fips-openssl
exit ${exit_code}
