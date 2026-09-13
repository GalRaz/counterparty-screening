"""Subject model. Identifiers carry provenance; nothing here is ever inferred."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

TYPES = ("person", "organization")


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    source: str

    def __post_init__(self):
        if not self.kind.strip():
            raise ValueError("identifier kind is required")
        if not self.value.strip():
            raise ValueError("identifier value is required")
        if not self.source.strip():
            raise ValueError("identifier source is required (§4: never invent one)")


def parse_identifier(text: str) -> Identifier:
    """CLI form `kind=value@source`."""
    m = re.fullmatch(r"([^=]+)=(.+?)@(.+)", text)
    if not m:
        raise ValueError(f"identifier must be kind=value@source, got {text!r}")
    return Identifier(m.group(1).strip(), m.group(2).strip(), m.group(3).strip())


def _squash(name: str) -> str:
    return " ".join(name.split())


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return s or "subject"


@dataclass
class Subject:
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    identifiers: list[Identifier] = field(default_factory=list)
    jurisdiction: str | None = None
    role: str | None = None
    os_schema: str | None = None  # explicit OpenSanctions schema override

    def __post_init__(self):
        if self.type not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        self.name = _squash(self.name)
        if not self.name:
            raise ValueError("name is required")
        self.aliases = [_squash(a) for a in self.aliases if _squash(a)]

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def normalised_key(self) -> str:
        key = f"{self.type}:{self.name.lower()}"
        if self.type == "person" and (dob := self.identifier("dob")):
            key += f":dob={dob}"
        if self.type == "organization" and (reg := self.identifier("registration_number")):
            key += f":reg={re.sub(r'\s+', '', reg)}"
        return key

    def all_names(self) -> list[str]:
        seen: list[str] = []
        for n in [self.name, *self.aliases]:
            if n not in seen:
                seen.append(n)
        return seen

    def identifier(self, kind: str) -> str | None:
        for i in self.identifiers:
            if i.kind == kind:
                return i.value
        return None

    def identifier_source(self, kind: str) -> str | None:
        for i in self.identifiers:
            if i.kind == kind:
                return i.source
        return None

    def has_non_latin_name(self) -> bool:
        for n in self.all_names():
            for ch in n:
                if ch.isalpha() and "LATIN" not in unicodedata.name(ch, "LATIN"):
                    return True
        return False

    def split_person_name(self) -> tuple[str, str, str]:
        parts = self.name.split()
        if len(parts) == 1:
            return "", "", parts[0]
        return parts[0], " ".join(parts[1:-1]), parts[-1]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Subject":
        d = dict(d)
        d["identifiers"] = [Identifier(**i) for i in d.get("identifiers", [])]
        return cls(**d)
