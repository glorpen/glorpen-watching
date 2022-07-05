import dataclasses
import datetime
import enum
import typing


class DuplicatedEntryException(ValueError):
    pass


@dataclasses.dataclass
class Label:
    id: typing.Optional[str]
    name: str
    color: str

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
    id: typing.Optional[str]
    name: typing.Optional[str]
    date: typing.Optional[datetime.date]


@dataclasses.dataclass
class List:
    id: typing.Optional[str]
    name: str
    items: typing.Sequence[ListItem]


@dataclasses.dataclass
class ParsedRawDescription:
    alt_titles: list[str]
    description: typing.Optional[str]
    source_url: str


@dataclasses.dataclass
class Card(ParsedRawDescription):
    id: typing.Optional[str]
    title: str
    labels: set[DataLabels]
    tags: typing.Set[Label]
    cover_url: typing.Optional[str]
    lists: typing.Optional[typing.Sequence[List]]

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
    cover: typing.Union[None, bytes, str]
    tags: typing.Set[str]
    description: typing.Optional[str]
    tags: set[str]
    labels: set[DataLabels]
    parts: dict[str, list[str]]
