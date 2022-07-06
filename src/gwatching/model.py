import dataclasses
import datetime
import enum
import typing


class DuplicatedEntryException(ValueError):
    pass


@dataclasses.dataclass
class Date:
    year: int
    month: typing.Optional[int] = None
    day: typing.Optional[int] = None

    def __str__(self):
        return "-".join([
            f"{self.year:04d}",
            "??" if self.month is None else f"{self.month:02d}",
            "??" if self.day is None else f"{self.day:02d}",
        ])


@dataclasses.dataclass
class Label:
    id: typing.Optional[str]
    name: str
    color: typing.Optional[str]

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other: 'Label'):
        return self.id == other.id


class DataLabels(enum.Enum):
    BOOKS = "książki"
    ANIME = "anime"
    SERIES = "series"
    MOVIE = "movie"
    CARTOON = "cartoon"
    AIRING_ENDED = "airing ended"


@dataclasses.dataclass
class ListItem:
    number: int
    id: typing.Optional[str] = None
    name: typing.Optional[str] = None
    date: typing.Optional[Date] = None

    def __repr__(self):
        return f"<ListItem: {self.number},{self.name},{self.date}>"


@dataclasses.dataclass
class List:
    name: str
    items: typing.Sequence[ListItem] = dataclasses.field(default_factory=list)
    id: typing.Optional[str] = None

    def __repr__(self):
        return f"<List: {self.name}>"


@dataclasses.dataclass
class ParsedRawDescription:
    alt_titles: typing.Sequence[str]
    source_url: str
    description: typing.Optional[str]


@dataclasses.dataclass
class Card:
    title: str
    source_url: str
    version: str
    alt_titles: typing.Sequence[str] = dataclasses.field(default_factory=list)
    description: typing.Optional[str] = None
    labels: set[DataLabels] = dataclasses.field(default_factory=set)
    tags: typing.Set[Label] = dataclasses.field(default_factory=set)
    lists: typing.Sequence[List] = dataclasses.field(default_factory=list)
    cover_id: typing.Optional[str] = None
    id: typing.Optional[str] = None

    @property
    def aired(self):
        return DataLabels.AIRING_ENDED in self.labels


@dataclasses.dataclass
class PendingCard:
    id: typing.Optional[str]
    name: str
    description: typing.Optional[str]
    labels: set[DataLabels]


@dataclasses.dataclass
class ScrappedData:
    titles: typing.Sequence[str]
    url: str
    tags: typing.Set[str]
    labels: typing.Set[DataLabels]
    parts: typing.Sequence[List]
    cover: typing.Optional[bytes] = None
    description: typing.Optional[str] = None
