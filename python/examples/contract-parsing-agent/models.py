from pydantic import BaseModel

__version__ = "0.1.0"


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls):
        return cls(version=__version__)
