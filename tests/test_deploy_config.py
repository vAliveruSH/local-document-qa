"""Checks that the hosting configuration files point at the read-only demo and are well formed."""
import importlib
import json
import tomllib
from pathlib import Path

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parent.parent


def test_vercel_entrypoint_is_the_read_only_demo():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(config) == {"tool"}  # no [project] table that could change how dependencies are installed
    module_name, attribute = config["tool"]["vercel"]["entrypoint"].split(":")
    assert (module_name, attribute) == ("docqa.demo", "app")
    app = getattr(importlib.import_module(module_name), attribute)
    assert isinstance(app, FastAPI) and "demo" in app.title


def test_vercel_json_targets_the_demo_file():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    (function_file, settings), = config["functions"].items()
    assert function_file == "docqa/demo.py" and (ROOT / function_file).is_file()
    assert "demo/**" in settings["includeFiles"] and "docqa/web/**" in settings["includeFiles"]
    assert "tests/**" in settings["excludeFiles"] and "data/**" in settings["excludeFiles"]


def test_python_version_is_supported_by_vercel():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() in {"3.12", "3.13", "3.14"}


def test_the_full_local_server_is_not_the_deployed_app():
    import docqa.demo as demo

    source = Path(demo.__file__).read_text(encoding="utf-8")
    assert "from .server" not in source and "create_app(" not in source
    assert "ArxivClient(" not in source and "OllamaGenerator(" not in source and "IngestWorker(" not in source
