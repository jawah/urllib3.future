FROM python:3.7.2-alpine3.7

RUN apk add build-base libffi-dev

WORKDIR /app

RUN pip install --upgrade pip setuptools
RUN pip install nox

ENV TRAEFIK_HTTPBIN_ENABLE=false
ENV CI=true

COPY ./src/urllib3 src/urllib3/
COPY ./test test/
COPY ./dummyserver dummyserver/
COPY ./ci ci/

COPY noxfile.py LICENSE.txt pyproject.toml README.md setup.cfg hatch_build.py dev-requirements.txt mypy-requirements.txt ./

CMD nox -s test-3.7
