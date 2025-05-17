from __future__ import annotations

import contextlib
import os
import platform
import random
import shutil
import string
import subprocess
import time
import typing
from http.client import RemoteDisconnected
from socket import timeout as SocketTimeout
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import nox


@contextlib.contextmanager
def traefik_boot(
    session: nox.Session, *args: str
) -> typing.Generator[None, None, None]:
    """
    Start a server to reliably test HTTP/1.1, HTTP/2 and HTTP/3 over QUIC.
    """
    # we may want to avoid starting the traefik server...
    if os.environ.get("TRAEFIK_HTTPBIN_ENABLE", "true") != "true":
        yield
        return

    # nox allows us to specify pos args
    # if we detect any of them, we should check
    # if the target tests requires Traefik or not.
    detect_specific_traefik: bool | None = None
    for arg in args:
        if arg.startswith("test/"):
            detect_specific_traefik = False
        if arg.startswith("test/with_traefik") or arg == "test/":
            detect_specific_traefik = True
            break
    if detect_specific_traefik is False:
        yield
        return

    external_stack_started = False
    is_windows = platform.system() == "Windows"
    dc_v1_legacy = is_windows is False and shutil.which("docker-compose") is not None
    traefik_ipv4 = os.environ.get("TRAEFIK_HTTPBIN_IPV4", "127.0.0.1")

    if dc_v1_legacy:
        dc_v2_probe = subprocess.Popen(["docker", "compose", "ps"])

        dc_v2_probe.wait()
        dc_v1_legacy = dc_v2_probe.returncode != 0

    if not os.path.exists("./traefik/httpbin.local.pem"):
        session.log("Prepare fake certificates for our Traefik server...")

        session.run(
            *[
                "python",
                "-m",
                "trustme",
                "-i",
                "httpbin.local",
                "alt.httpbin.local",
                "-d",
                "./traefik",
            ]
        )

        shutil.move("./traefik/server.pem", "./traefik/httpbin.local.pem")

        if os.path.exists("./traefik/httpbin.local.pem.key"):
            os.unlink("./traefik/httpbin.local.pem.key")

        shutil.move("./traefik/server.key", "./traefik/httpbin.local.pem.key")

        if os.path.exists("./rootCA.pem"):
            os.unlink("./rootCA.pem")

        shutil.move("./traefik/client.pem", "./rootCA.pem")

    try:
        session.log("Attempt to start Traefik with go-httpbin[...]")

        if is_windows:
            if not os.path.exists("./go-httpbin"):
                clone_proc = subprocess.Popen(
                    ["git", "clone", "https://github.com/mccutchen/go-httpbin.git"]
                )

                clone_proc.wait()

            shutil.copyfile(
                "./traefik/patched.Dockerfile", "./go-httpbin/patched.Dockerfile"
            )

            dc_process = subprocess.Popen(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.win.yaml",
                    "up",
                    "-d",
                ]
            )
        else:
            if dc_v1_legacy:
                dc_process = subprocess.Popen(["docker-compose", "up", "-d"])
            else:
                dc_process = subprocess.Popen(["docker", "compose", "up", "-d"])

        dc_process.wait()
    except OSError as e:
        session.warn(
            f"Traefik server cannot be run due to an error with containers: {e}"
        )
    else:
        session.log("Traefik server is starting[...]")

        i = 0

        while True:
            if i >= 120:
                if not dc_v1_legacy:
                    subprocess.Popen(
                        [
                            "docker",
                            "compose",
                            "-f",
                            "docker-compose.win.yaml",
                            "logs",
                            "--tail=128",
                        ]
                    )

                raise TimeoutError(
                    "Error while waiting for the Traefik server (timeout/readiness)"
                )

            try:
                r = urlopen(
                    Request(
                        f"http://{traefik_ipv4}:8888/get",
                        headers={"Host": "httpbin.local"},
                    ),
                    timeout=1.0,
                )
            except (
                HTTPError,
                URLError,
                RemoteDisconnected,
                TimeoutError,
                SocketTimeout,
                ConnectionError,
            ) as e:
                i += 1
                time.sleep(1)
                session.log(f"Waiting for the Traefik server: {e}...")
                continue

            if int(r.status) == 200:
                break

        session.log("Traefik server is ready to accept connections[...]")
        external_stack_started = True

    yield

    if external_stack_started:
        if dc_v1_legacy:
            dc_process = subprocess.Popen(["docker-compose", "stop"])
        else:
            dc_process = subprocess.Popen(["docker", "compose", "stop"])

        dc_process.wait()


def tests_impl(
    session: nox.Session,
    extras: str = "socks,brotli,zstd,ws",
    byte_string_comparisons: bool = False,
    tracemalloc_enable: bool = False,
) -> None:
    # Install deps and the package itself.
    session.install("-U", "pip", "setuptools", silent=False)
    session.install("-r", "dev-requirements.txt", silent=False)

    with traefik_boot(session, *session.posargs):
        session.install(f".[{extras}]", silent=False)

        # Show the pip version.
        session.run("pip", "--version")
        # Print the Python version and bytesize.
        session.run("python", "--version")
        session.run("python", "-c", "import struct; print(struct.calcsize('P') * 8)")
        session.run("python", "-c", "import ssl; print(ssl.OPENSSL_VERSION)")

        # Inspired from https://hynek.me/articles/ditch-codecov-python/
        # We use parallel mode and then combine in a later CI step
        session.run(
            "python",
            *(("-bb",) if byte_string_comparisons else ()),
            "-m",
            "pytest",
            "-n",
            "2" if os.environ.get("CI") else "4",
            "--cov",
            "urllib3",
            "-v",
            "-ra",
            f"--color={'yes' if 'GITHUB_ACTIONS' in os.environ else 'auto'}",
            "--tb=native",
            "--durations=10",
            "--strict-config",
            "--strict-markers",
            *(session.posargs or ("test/",)),
            env={
                "PYTHONWARNINGS": "always::DeprecationWarning",
                "COVERAGE_CORE": "sysmon",
                "PYTHONTRACEMALLOC": "25" if tracemalloc_enable else "",
            },
        )

    suffix = "".join(
        random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits)
        for _ in range(8)
    )
    os.rename(".coverage", f".coverage.{suffix}")


@nox.session(
    python=["3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "pypy"]
)
def test(session: nox.Session) -> None:
    tests_impl(session)


@nox.session(python=["3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"])
def tracemalloc(session: nox.Session) -> None:
    tests_impl(session, tracemalloc_enable=True)


@nox.session(python=["3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"])
def test_ssl_large_resources(session: nox.Session) -> None:
    # Install deps and the package itself.
    session.install("-U", "pip", "setuptools", silent=False)
    session.install("-r", "dev-requirements.txt", silent=False)
    session.install(".", silent=False)

    # Show the pip version.
    session.run("pip", "--version")
    # Print the Python version and bytesize.
    session.run("python", "--version")
    session.run("python", "-c", "import struct; print(struct.calcsize('P') * 8)")
    session.run("python", "-c", "import ssl; print(ssl.OPENSSL_VERSION)")

    session.run(
        "python",
        "-m",
        "coverage",
        "run",
        "--parallel-mode",
        "-m",
        "pytest",
        "-v",
        "-ra",
        f"--color={'yes' if 'GITHUB_ACTIONS' in os.environ else 'auto'}",
        "--tb=native",
        "--strict-config",
        "--strict-markers",
        "test/with_dummyserver/test_socketlevel.py::TestSSL::test_requesting_large_resources_via_ssl",
        env={
            "PYTHONWARNINGS": "always::DeprecationWarning",
            "COVERAGE_CORE": "sysmon",
            "CI": None,
        },
    )


@nox.session(python=["3"])
def test_brotlipy(session: nox.Session) -> None:
    """Check that if 'brotlipy' is installed instead of 'brotli' or
    'brotlicffi' that we still don't blow up.
    """
    session.install("brotlipy")
    tests_impl(session, extras="socks", byte_string_comparisons=False)


def git_clone(session: nox.Session, git_url: str) -> None:
    """We either clone the target repository or if already exist
    simply reset the state and pull.
    """
    expected_directory = git_url.split("/")[-1]

    if expected_directory.endswith(".git"):
        expected_directory = expected_directory[:-4]

    if not os.path.isdir(expected_directory):
        session.run("git", "clone", "--depth", "1", git_url, external=True)
    else:
        session.run(
            "git", "-C", expected_directory, "reset", "--hard", "HEAD", external=True
        )
        session.run("git", "-C", expected_directory, "pull", external=True)


@nox.session()
def downstream_botocore(session: nox.Session) -> None:
    root = os.getcwd()
    tmp_dir = session.create_tmp()

    session.cd(tmp_dir)
    git_clone(session, "https://github.com/boto/botocore")
    session.chdir("botocore")
    for patch in [
        "0001-Mark-100-Continue-tests-as-failing.patch",
        "0003-Mark-HttpConn-bypass-internals-as-xfail.patch",
    ]:
        session.run("git", "apply", f"{root}/ci/{patch}", external=True)
    session.run("git", "rev-parse", "HEAD", external=True)
    session.run("python", "scripts/ci/install")

    session.cd(root)

    session.install(".", silent=False)
    session.cd(f"{tmp_dir}/botocore")

    session.run("python", "-c", "import urllib3; print(urllib3.__version__)")
    session.run("python", "scripts/ci/run-tests")


@nox.session()
def downstream_niquests(session: nox.Session) -> None:
    root = os.getcwd()
    tmp_dir = session.create_tmp()

    session.cd(tmp_dir)
    git_clone(session, "https://github.com/jawah/niquests")
    session.chdir("niquests")

    session.run("git", "rev-parse", "HEAD", external=True)
    session.install(".[socks]", silent=False)
    session.install("-r", "requirements-dev.txt", silent=False)

    session.cd(root)
    session.install(".", silent=False)
    session.cd(f"{tmp_dir}/niquests")

    session.run("python", "-c", "import urllib3; print(urllib3.__version__)")
    session.run(
        "python",
        "-m",
        "pytest",
        "-v",
        f"--color={'yes' if 'GITHUB_ACTIONS' in os.environ else 'auto'}",
        *(session.posargs or ("tests/",)),
        env={"NIQUESTS_STRICT_OCSP": "1"},
    )


@nox.session()
def downstream_requests(session: nox.Session) -> None:
    root = os.getcwd()
    tmp_dir = session.create_tmp()

    session.cd(tmp_dir)
    git_clone(session, "https://github.com/psf/requests")
    session.chdir("requests")

    for patch in [
        "0004-Requests-ChunkedEncodingError.patch",
    ]:
        session.run("git", "apply", f"{root}/ci/{patch}", external=True)

    session.run("git", "rev-parse", "HEAD", external=True)
    session.install(".[socks]", silent=False)
    session.install("-r", "requirements-dev.txt", silent=False)

    session.cd(root)
    session.install(".", silent=False)
    session.cd(f"{tmp_dir}/requests")

    session.run("python", "-c", "import urllib3; print(urllib3.__version__)")
    session.run(
        "python",
        "-m",
        "pytest",
        "-v",
        f"--color={'yes' if 'GITHUB_ACTIONS' in os.environ else 'auto'}",
        *(session.posargs or ("tests/",)),
    )


@nox.session()
def downstream_boto3(session: nox.Session) -> None:
    root = os.getcwd()
    tmp_dir = session.create_tmp()

    session.cd(tmp_dir)
    git_clone(session, "https://github.com/boto/boto3")
    session.chdir("boto3")

    session.run("git", "rev-parse", "HEAD", external=True)
    session.install(".", silent=False)
    session.install("-r", "requirements-dev.txt", silent=False)

    session.cd(root)
    session.install(".", silent=False)
    session.cd(f"{tmp_dir}/boto3")

    session.run("python", "-c", "import urllib3; print(urllib3.__version__)")
    session.run(
        "python",
        "scripts/ci/run-tests",
    )


@nox.session()
def downstream_sphinx(session: nox.Session) -> None:
    root = os.getcwd()
    tmp_dir = session.create_tmp()

    session.cd(tmp_dir)
    git_clone(session, "https://github.com/sphinx-doc/sphinx")
    session.chdir("sphinx")

    session.run("git", "rev-parse", "HEAD", external=True)
    session.install(".[test]", silent=False)

    session.cd(root)
    session.install(".", silent=False)
    session.cd(f"{tmp_dir}/sphinx")

    session.run("python", "-c", "import urllib3; print(urllib3.__version__)")
    session.run(
        "python",
        "-m",
        "pytest",
        "-v",
        f"--color={'yes' if 'GITHUB_ACTIONS' in os.environ else 'auto'}",
        *(session.posargs or ("tests/",)),
    )


@nox.session()
def downstream_docker(session: nox.Session) -> None:
    root = os.getcwd()
    tmp_dir = session.create_tmp()

    session.cd(tmp_dir)
    git_clone(session, "https://github.com/docker/docker-py")
    session.chdir("docker-py")

    for patch in [
        "0005-DockerPy-FixBadChunk.patch",
    ]:
        session.run("git", "apply", f"{root}/ci/{patch}", external=True)

    session.run("git", "rev-parse", "HEAD", external=True)
    session.install(".[ssh,dev]", silent=False)

    session.cd(root)
    session.install(".", silent=False)
    session.cd(f"{tmp_dir}/docker-py")

    session.run("python", "-c", "import urllib3; print(urllib3.__version__)")
    session.run(
        "python",
        "-m",
        "pytest",
        "-v",
        f"--color={'yes' if 'GITHUB_ACTIONS' in os.environ else 'auto'}",
        *(session.posargs or ("tests/unit",)),
    )


@nox.session()
def format(session: nox.Session) -> None:
    """Run code formatters."""
    lint(session)


@nox.session
def lint(session: nox.Session) -> None:
    session.install("pre-commit")
    session.run("pre-commit", "run", "--all-files")

    mypy(session)


@nox.session
def mypy(session: nox.Session) -> None:
    """Run mypy."""
    session.install("-r", "mypy-requirements.txt")
    session.run("mypy", "--version")
    session.run(
        "mypy",
        "dummyserver",
        "noxfile.py",
        "src/urllib3",
        "test",
    )


@nox.session
def docs(session: nox.Session) -> None:
    session.install("-r", "docs/requirements.txt")
    session.install(".[socks,brotli,zstd,ws]")

    session.chdir("docs")
    if os.path.exists("_build"):
        shutil.rmtree("_build")
    session.run("sphinx-build", "-b", "html", "-W", ".", "_build/html")
