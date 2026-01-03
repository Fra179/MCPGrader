from .configs import ProgramConfig
from typing import Any
import yaml
import os

class ConfigParser:
    def __init__(self, config_file: str):
        assert config_file.endswith(".yaml") or config_file.endswith(".yml"), "Config file must be a YAML file."
        assert os.path.isfile(config_file), f"Config file '{config_file}' does not exist."
        self.__config_file = config_file
        self.__config = None

    def parse(self) -> None:
        with open(self.__config_file, "r") as file:
            content: str = file.read()

        data: Any = yaml.safe_load(content)
        program_config: ProgramConfig = ProgramConfig.from_dict(data)
        program_config.assert_valid()

        self.__config = program_config

    @property
    def config(self) -> ProgramConfig:
        return self.__config