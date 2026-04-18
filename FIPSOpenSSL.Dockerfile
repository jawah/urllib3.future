FROM filigran/python-fips:latest

RUN openssl list -providers

RUN sed -i 's|^alg_section = algorithm_sect|alg_section = algorithm_sect\nrandom = random_sect|' \
        /etc/ssl/openssl.cnf \
 && printf '\n[random_sect]\nrandom = CTR-DRBG\ncipher = AES-256-CTR\n' \
        >> /etc/ssl/openssl.cnf \
 && openssl rand -hex 16 > /dev/null

WORKDIR /app

RUN pip install --upgrade pip setuptools
RUN pip install nox

ENV TRAEFIK_HTTPBIN_ENABLE=false
ENV CI=true
ENV TERM=xterm-256color
ENV FIPS_MODE=1

COPY ./src/urllib3 src/urllib3/
COPY ./test test/
COPY ./dummyserver dummyserver/
COPY ./ci ci/

COPY noxfile.py LICENSE.txt pyproject.toml README.md hatch_build.py dev-requirements.txt mypy-requirements.txt urllib3_future.pth ./

CMD nox -s test-3.12
