from __future__ import annotations

from os import environ, path
from shutil import copytree, rmtree
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SHOULD_PREVENT_FORK_OVERRIDE = environ.get("URLLIB3_NO_OVERRIDE", None) is not None


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        #: Clean-up in case of previously missed proper exit
        if path.exists("./src/urllib3_future"):
            rmtree("./src/urllib3_future")

        #: Copying the main package and duplicate it. Provide an escape hatch.
        copytree("./src/urllib3", "./src/urllib3_future")

        #: Aimed at OS package manager, so that they don't override accidentally urllib3.
        if SHOULD_PREVENT_FORK_OVERRIDE and path.exists("./src/urllib3"):
            rmtree("./src/urllib3")

        #: The PTH autorun script is made to ensure consistency / reproducibility
        #: across many configuration.
        if (
            self.target_name != "wheel"
            or SHOULD_PREVENT_FORK_OVERRIDE
            or version == "editable"
        ):
            return

        build_data["force_include"]["urllib3_future.pth"] = "urllib3_future.pth"

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        #: We shall restore the original package before exiting
        if SHOULD_PREVENT_FORK_OVERRIDE and not path.exists("./src/urllib3"):
            copytree("./src/urllib3_future", "./src/urllib3")

        #: Removing the temporary duplicate
        if path.exists("./src/urllib3_future"):
            rmtree("./src/urllib3_future")
