"""Package a Lambda folder without Docker. Owner: role 5.

CDK normally bundles Python Lambdas inside a Docker container. Nobody on the
team should need Docker for this project, so this copies the handler folder
and pip-installs its requirements.txt for the Lambda platform on the host.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import ILocalBundling

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "tests", ".DS_Store")


@jsii.implements(ILocalBundling)
class LocalPipBundler:
    def __init__(self, source: Path, python_version: str = "3.12") -> None:
        self.source = source
        self.python_version = python_version

    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        shutil.copytree(self.source, output_dir, ignore=IGNORE, dirs_exist_ok=True)
        req = self.source / "requirements.txt"
        wants_packages = req.exists() and any(
            line.strip() and not line.startswith("#") for line in req.read_text().splitlines()
        )
        if wants_packages:
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install", "-q",
                    "-r", str(req), "-t", output_dir,
                    "--platform", "manylinux2014_x86_64",
                    "--implementation", "cp",
                    "--python-version", self.python_version,
                    "--only-binary=:all:",
                    "--upgrade",
                ],
                check=True,
            )
        return True
