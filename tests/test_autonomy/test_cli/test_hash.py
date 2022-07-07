# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2022 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Test hash command group."""


import shutil
from pathlib import Path
from typing import Dict, Tuple

from autonomy.configurations.loader import load_service_config

from tests.conftest import ROOT_DIR
from tests.test_autonomy.test_cli.base import BaseCliTest


class TestHashAll(BaseCliTest):
    """Test `hash-all` command."""

    packages_dir: Path
    cli_options: Tuple[str, ...] = (
        "hash",
        "all",
    )

    @classmethod
    def setup(cls) -> None:
        """Setup class."""

        super().setup()

        cls.packages_dir = cls.t / "packages"
        shutil.copytree(ROOT_DIR / "packages", cls.packages_dir)

    def load_hashes(
        self,
    ) -> Dict[str, str]:
        """Load hashes from CSV file."""

        hashes_file = self.packages_dir / "hashes.csv"
        with open(str(hashes_file), "r") as file:
            content = file.read().strip()

        return dict([line.split(",") for line in content.split("\n") if "," in line])  # type: ignore

    def test_service_hashing(
        self,
    ) -> None:
        """Check if `hash-all` updates agent hashes properly."""

        service_name = "counter"
        result = self.run_cli(("--packages-dir", str(self.packages_dir)))

        assert result.exit_code == 0

        service_path = self.packages_dir / "valory" / "services" / service_name
        service_config = load_service_config(service_path)
        hashes = self.load_hashes()
        key = f"valory/agents/{service_name}"

        assert key in hashes, (
            hashes,
            service_config.agent,
        )
        assert hashes[key] == service_config.agent.hash

        result = self.run_cli(("--packages-dir", str(self.packages_dir), "--check"))

        assert result.exit_code == 0, result.output
        assert "OK!" in result.output, result.output
