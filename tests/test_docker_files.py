"""Phase 15: guards for the Docker packaging. They cannot build an image (that needs Docker running), but they catch the mistakes that
break one or leak something: a package list that drifted from requirements.txt, a COPY of a path that does not exist, a shell script
with Windows line endings, a database port published to the network, a secret baked into the image.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(re.sub(r"\s+", "", line))
    return lines


def package_name(line: str) -> str:
    return re.split(r"[<>=\[!~;]", line, maxsplit=1)[0].lower()


# ------------------------------------------------------------------ packages
def test_every_docker_requirement_is_the_same_line_as_in_requirements_txt():
    full, docker = requirement_lines(ROOT / "requirements.txt"), requirement_lines(ROOT / "docker" / "requirements-docker.txt")
    assert docker and not [line for line in docker if line not in full], "docker/requirements-docker.txt drifted from requirements.txt"


def test_the_image_leaves_out_test_tools_and_spark_unless_asked():
    docker = {package_name(line) for line in requirement_lines(ROOT / "docker" / "requirements-docker.txt")}
    assert not docker & {"pytest", "httpx", "pypdfium2", "pyspark", "plotly"}
    assert "WITH_SPARK" in DOCKERFILE and "openjdk-21-jre-headless" in DOCKERFILE and "pyspark" in DOCKERFILE      # opt-in, with Java


def test_everything_the_app_needs_at_run_time_is_in_the_image_list():
    needed = {"fastapi", "uvicorn", "pymongo", "pandas", "numpy", "ta", "openpyxl", "reportlab", "pyarrow", "psutil", "pydantic-settings", "python-multipart"}
    assert needed <= {package_name(line) for line in requirement_lines(ROOT / "docker" / "requirements-docker.txt")}


# ------------------------------------------------------------------ the Dockerfile
def test_every_path_the_dockerfile_copies_exists():
    copies = [line.split()[1:-1] for line in DOCKERFILE.splitlines() if line.startswith("COPY ") and "--from" not in line]
    sources = [s for group in copies for s in group]
    assert {"backend", "scripts", "frontend/", "docker/requirements-docker.txt", "docker/entrypoint.sh", "frontend/package.json"} <= set(sources)
    for source in sources:
        assert (ROOT / source.rstrip("/")).exists(), f"COPY {source}: no such path in the project"


def test_the_container_does_not_run_as_root_and_checks_its_own_health():
    assert re.search(r"^USER app$", DOCKERFILE, re.M) and "useradd" in DOCKERFILE
    assert "HEALTHCHECK" in DOCKERFILE and "/api/health" in DOCKERFILE
    assert re.search(r'^CMD \["uvicorn", "app.main:app"', DOCKERFILE, re.M) and 'ENTRYPOINT ["/entrypoint.sh"]' in DOCKERFILE


def test_the_dashboard_is_built_in_a_separate_stage_so_node_is_not_in_the_final_image():
    stages = re.findall(r"^FROM (\S+) AS (\w+)", DOCKERFILE, re.M)
    assert [name for _, name in stages] == ["dashboard", "app"] and stages[0][0].startswith("node:") and stages[1][0].startswith("python:3.11")
    assert "COPY --from=dashboard" in DOCKERFILE and "npm ci" in DOCKERFILE


def test_the_entry_script_has_unix_line_endings_and_a_shebang():
    data = (ROOT / "docker" / "entrypoint.sh").read_bytes()
    assert b"\r" not in data, "CRLF line endings would make the script fail inside Linux ('/bin/sh^M: not found')"
    assert data.startswith(b"#!/bin/sh\n") and b"set -e" in data and b'exec "$@"' in data
    assert "eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


# ------------------------------------------------------------------ docker-compose.yml
def test_compose_has_the_two_services_and_waits_for_a_healthy_database():
    services = COMPOSE["services"]
    assert set(services) == {"mongo", "app"}
    assert services["app"]["depends_on"]["mongo"]["condition"] == "service_healthy" and "healthcheck" in services["mongo"]
    assert services["app"]["build"]["dockerfile"] == "docker/Dockerfile"


def test_the_database_is_not_published_and_the_app_listens_on_this_computer_only():
    services = COMPOSE["services"]
    assert "ports" not in services["mongo"], "MongoDB has no login: it must stay on the private compose network"
    assert services["app"]["ports"] == ["127.0.0.1:${APP_PORT:-8000}:8000"]
    assert services["app"]["environment"]["MONGO_URI"] == "mongodb://mongo:27017"


def test_no_secret_is_written_into_the_compose_file_or_the_image():
    env = COMPOSE["services"]["app"]["environment"]
    assert env["API_KEY"] == "${API_KEY:-}"                                   # taken from the caller's environment, empty by default
    assert not re.search(r"(?i)(password|secret|token)\s*[:=]\s*[^\s$]", (ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "data", ".git", "kaggle.json"} <= set(ignore) and "!.env.example" in ignore      # secrets and data are never copied in
    assert "COPY .env" not in DOCKERFILE and "COPY . " not in DOCKERFILE      # no blanket copy that could pick up a stray file


def test_data_and_database_live_in_volumes_so_they_survive_a_rebuild():
    assert {"mongo-data", "app-logs"} <= set(COMPOSE["volumes"])
    assert "mongo-data:/data/db" in COMPOSE["services"]["mongo"]["volumes"] and "./data:/app/data" in COMPOSE["services"]["app"]["volumes"]
