FROM python:3.13-alpine

WORKDIR /app

RUN pip install --upgrade pip setuptools
RUN pip install nox coverage

ENV TRAEFIK_HTTPBIN_ENABLE=false
ENV CI=true
ENV TERM=xterm-256color

COPY ./src/urllib3 src/urllib3/
COPY ./test test/
COPY ./dummyserver dummyserver/
COPY ./ci ci/

COPY noxfile.py LICENSE.txt pyproject.toml README.md hatch_build.py dev-requirements.txt mypy-requirements.txt .coveragerc ./

CMD nox -s test-3.13 && python -m coverage combine && coverage report --ignore-errors --show-missing --fail-under=80
