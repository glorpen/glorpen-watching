import abc
import functools
import io
import itertools
import logging
import re
import typing

import PIL.Image
import more_itertools
import requests
from requests_oauthlib.oauth1_session import OAuth1Session

from glorpen.watching.model import Card, DataLabels, Date, DuplicatedEntryException, Label, List, ListItem, \
    ParsedRawDescription, \
    PendingCard, ScrappedData

api_host = 'api.trello.com'
api_version = 1

VERSION = "0.0.3"
MAX_TRELLO_LIST_SIZE = 200

class ApiException(Exception):
    @classmethod
    def raise_for_status(cls, response: requests.Response):
        if 400 <= response.status_code < 600:
            raise cls(f'{response.status_code} Api Error: {response.reason} for url: {response.url}, got: {response.content}')

        return response

class DescriptionParser(abc.ABC):
    def parse_description(self, description: str) -> ParsedRawDescription:
        raise NotImplementedError()

    def parse_checklist_item(self, item: dict) -> ListItem:
        raise NotImplementedError()


class DescriptionParserV0(DescriptionParser):
    _re_checklist_item = re.compile(
        r"\*\*(?P<number>-?\d+)\*\*(:?:\s*(:?\*(?P<name>.*)\*)?\s*(:?\[(?P<date>[\d-]+)\])?)?"
    )

    @classmethod
    def parse_date(cls, text: typing.Optional[str]):
        if text is None:
            return None
        return Date(*more_itertools.map_except(int, text.split("-"), ValueError))

    def parse_checklist_item(self, item: dict) -> ListItem:
        if item["name"].startswith("*"):
            m = self._re_checklist_item.match(item["name"])
            if not m:
                raise Exception(f"Could not parse {item['name']}")
            parts = m.groupdict()
            return ListItem(
                id=item["id"],
                number=int(parts["number"]),
                date=self.parse_date(parts["date"]),
                name=parts["name"]
            )
        else:
            return ListItem(
                id=item["id"],
                number=int(item["name"]),
                date=None,
                name=None
            )

    def _parse_source_url(self, description_lines: list[str]):
        return description_lines[-1].split('(')[-1][:-1]

    def parse_description(self, description: str):
        lines = description.splitlines(keepends=False)
        pos_after_alt_titles = 0
        alt_titles = []
        if lines[0].startswith("> Alt title:"):
            alt_titles.append(lines[0][14:-1])
            pos_after_alt_titles += 2
        elif lines[0].startswith("> Alt titles:"):
            for line in lines[1:]:
                if line.startswith("> "):
                    alt_titles.append(line[3:-1])
                    pos_after_alt_titles += 1
                else:
                    break
            pos_after_alt_titles += 1

        snip_index = lines.index("---")
        description = "\n".join(lines[pos_after_alt_titles:snip_index]).strip() or None
        source_url = self._parse_source_url(lines)

        return ParsedRawDescription(
            description=description,
            source_url=source_url,
            alt_titles=alt_titles
        )


class DescriptionParserV1(DescriptionParserV0):
    def _parse_source_url(self, description_lines: list[str]):
        return description_lines[-2].split('(')[-1][:-1]


class LabelBag:
    def __init__(self, labels: typing.Optional[typing.Iterable[Label]] = None):
        super(LabelBag, self).__init__()
        self._by_id: dict[str, Label] = {}
        self._by_names: dict[str, Label] = {}

        if labels:
            for label in labels:
                self.add(label)

    def add(self, label: Label):
        if label.id in self._by_id:
            raise DuplicatedEntryException(f"Label with id {label.id} already exist")
        if label.name in self._by_names:
            raise DuplicatedEntryException(f"Label with title {label.name} already exist")

        self._by_id[label.id] = self._by_names[label.name] = label

    def by_id(self, id: str):
        return self._by_id[id]

    def by_name(self, name: typing.Union[str, DataLabels]):
        if isinstance(name, DataLabels):
            name = name.value
        return self._by_names[name]

    def __iter__(self):
        return iter(self._by_id.values())

    def data(self) -> set[DataLabels]:
        return set(more_itertools.filter_except(self.by_name, DataLabels, KeyError))

    def tags(self) -> set[Label]:
        data_labels = set(label.value for label in DataLabels)
        tags = set()
        for key, value in self._by_names.items():
            if key not in data_labels:
                tags.add(value)
        return tags

    def __len__(self):
        return len(self._by_id)


class CardBag:
    def __init__(self):
        super(CardBag, self).__init__()
        self._by_id: dict[str, Card] = {}
        self._by_source_url: dict[str, Card] = {}
        self._pending: dict[str, PendingCard] = {}

    def add(self, card: Card):
        if card.id in self._by_id:
            raise DuplicatedEntryException(f"Card with id {card.id} already exist")
        if card.source_url in self._by_source_url:
            raise DuplicatedEntryException(
                f"Card with source url {card.source_url} already exist: {card.id} vs. {self._by_source_url[card.source_url].id}"
            )

        self._by_id[card.id] = self._by_source_url[card.source_url] = card

        if card.id in self._pending:
            del self._pending[card.id]

    def add_pending(self, card: PendingCard):
        self._pending[card.id] = card

    def get_pending(self):
        return self._pending.values()

    def by_id(self, id: str):
        return self._by_id[id]

    def by_source_url(self, url: str):
        return self._by_source_url[url]

    def has_source_url(self, url: str):
        return url in self._by_source_url

    def __len__(self):
        return len(self._by_id)

    def __iter__(self):
        return iter(self._by_id.values())


class DataFormatter:
    _re_card_source_url_host = re.compile(r'^[a-z]+://([^/]+).*$')

    @classmethod
    def format_cover(cls, cover: bytes):
        with PIL.Image.open(io.BytesIO(cover)) as im:
            tmp = io.BytesIO()
            im.save(tmp, "JPEG")

            # mime_type = mimetypes.guess_type("file.%s" % im.format)[0]

            return tmp.getvalue(), "image/jpeg"

    def format_description(self, source_url: str, card: typing.Union[ScrappedData, Card]):
        lines = []

        if hasattr(card, "alt_titles"):
            alt_titles = card.alt_titles
        else:
            alt_titles = card.titles[1:]

        if alt_titles:
            if len(alt_titles) > 1:
                lines.append("> Alt titles:\n%s\n\n" % ("\n".join("> *%s*" % i for i in alt_titles)))
            else:
                lines.append(f"> Alt title: *{alt_titles[0]}*\n\n")

        if card.description:
            lines.extend([card.description, ""])

        source_name = self._re_card_source_url_host.match(source_url).group(1)
        if len(source_name) > 46:
            source_name = source_name[:46] + "..."
        else:
            source_name = source_name

        lines.extend(
            [
                "---",
                "",
                f"Source: [{source_name}]({source_url})",
                f"Version: {VERSION}"
            ]
        )

        return "\n".join(lines)

    @classmethod
    def format_labels(cls, label_bag: LabelBag, labels: typing.Iterable[DataLabels], tags: typing.Iterable[Label]):
        return set(label.id for label in itertools.chain(map(label_bag.by_name, labels), tags))

    @classmethod
    def format_item(cls, item: ListItem):
        if item.name or item.date:
            parts = [f"**{item.number:02d}**:"]
            if item.name:
                parts.append(f"*{item.name}*")
            if item.date:
                parts.append(f"[{item.date}]")

            return " ".join(parts)
        else:
            return f"{item.number:02d}"


class UnknownVersionException(Exception):
    pass


class VersionDetector:
    _re_card_version = re.compile(r'^Version: ([\d.]+)$', re.MULTILINE)

    _parsers = {
        "0.0.1": DescriptionParserV1(),
        "0.0.2": DescriptionParserV1(),
        "0.0.3": DescriptionParserV1()
    }

    def get_version(self, description: str):
        m = self._re_card_version.findall(description)
        if not m:
            raise UnknownVersionException(f"Unknown version in: {description}")
        return m[0]

    def get_parser(self, version: str) -> DescriptionParser:
        return self._parsers[version]


class Database:
    _session: OAuth1Session
    _board_id: str

    def __init__(self, app_key: str, app_secret: str, version_detector: VersionDetector, data_formatter: DataFormatter):
        super(Database, self).__init__()

        self._app_key = app_key
        self._app_secret = app_secret
        self._logger = logging.root.getChild(self.__class__.__name__)
        self._version_detector = version_detector
        self._formatter = data_formatter

    def login(self, owner_key: str, owner_secret: str, board_id: str):
        self._session = OAuth1Session(
            self._app_key, client_secret=self._app_secret, resource_owner_key=owner_key,
            resource_owner_secret=owner_secret
        )

        self._session.get(f'{self._url}/tokens/{owner_key}')

        self._board_id = board_id

    @functools.cached_property
    def _url(self):
        return f'https://{api_host}/{api_version}'

    @functools.cached_property
    def _labels(self):
        labels = LabelBag()
        for label_info in self._get_api_labels():
            del label_info["idBoard"]
            labels.add(Label(**label_info))
        return labels

    def _get_api_cards(self):
        return self._session.get(f"{self._url}/boards/{self._board_id}/cards/all").json()

    def _get_api_labels(self):
        return self._session.get(f'{self._url}/boards/{self._board_id}/labels?limit=1000').json()

    @functools.cached_property
    def cards(self):
        cards = CardBag()
        checklists = dict()

        for checklist in self._session.get(f"{self._url}/boards/{self._board_id}/checklists").json():
            checklists[checklist["id"]] = checklist

        for card in self._get_api_cards():
            try:
                labels = LabelBag(self._labels.by_id(i) for i in card["idLabels"])

                try:
                    version = self._version_detector.get_version(card["desc"])
                except UnknownVersionException:
                    self._logger.info(f"Version not found in {card['id']} / {card['name']}")
                    cards.add_pending(
                        PendingCard(
                            description=card['desc'],
                            id=card['id'],
                            name=card['name'],
                            labels=labels.data()
                        )
                    )
                    continue

                parser = self._version_detector.get_parser(version)
                parsed_description = parser.parse_description(card["desc"])

                if card['cover']:
                    cover_id = card['cover']['idAttachment']
                else:
                    cover_id = None

                card_checklists = []

                hashed_lists = dict(
                    (checklists[checklist_id]["pos"], checklists[checklist_id]) for checklist_id in card["idChecklists"]
                )
                for list_pos in sorted(hashed_lists.keys()):
                    checklist = hashed_lists[list_pos]
                    card_checklists.append(
                        List(
                            name=checklist["name"],
                            id=checklist["id"],
                            items=list(
                                parser.parse_checklist_item(item) for item in
                                sorted(checklist["checkItems"], key=lambda x: x["pos"])
                            )
                        )
                    )

                card_model = Card(
                    id=card["id"],
                    lists=card_checklists,
                    cover_id=cover_id,
                    title=card["name"],
                    tags=labels.tags(),
                    labels=labels.data(),
                    version=version,
                    **vars(parsed_description)
                )
                cards.add(card_model)
            except Exception as e:
                raise Exception(f"Could not load card {card['id']}") from e

        return cards

    def fix_labels(self):
        known_labels: dict[str, str] = dict()
        duplicated_labels: dict[str, list[str]] = dict()
        used_labels: set[str] = set()
        empty_labels: set[str] = set()

        for label in self._get_api_labels():
            name = label["name"]
            id = label["id"]
            if not name:
                empty_labels.add(id)
            else:
                if name in known_labels:
                    duplicated_labels.setdefault(name, []).append(id)
                else:
                    known_labels[name] = id

        self._logger.warning(f"Found {len(duplicated_labels)} duplicated labels")

        for card in self._get_api_cards():
            for label in card["labels"]:
                name = label["name"]
                id = label["id"]
                if name in duplicated_labels and id in duplicated_labels[name]:
                    self._logger.warning(f'Replacing label "{name}" on card {card["id"]} / {card["name"]}')
                    # add new label
                    ApiException.raise_for_status(self._session.post(
                        f"{self._url}/cards/{card['id']}/idLabels", params={
                            "value": known_labels[name]
                        }
                    ))
                    # remove duplicated label
                    ApiException.raise_for_status(self._session.delete(f"{self._url}/cards/{card['id']}/idLabels/{id}"))
                    used_labels.add(known_labels[name])
                else:
                    used_labels.add(id)

        if duplicated_labels:
            self._logger.warning(f"Removing duplicated labels")
            for labels in duplicated_labels.values():
                for label_id in labels:
                    self._logger.warning(f"Removing duplicated label {label_id}")
                    ApiException.raise_for_status(self._session.delete(f"{self._url}/labels/{label_id}"))

        unused_labels = set(known_labels.values()).difference(used_labels)
        self._logger.warning(f"Found {len(unused_labels)} unused labels")
        for label_id in unused_labels:
            self._logger.warning(f"Removing unused label {label_id}")
            ApiException.raise_for_status(self._session.delete(f"{self._url}/labels/{label_id}"))

        self._logger.warning(f"Removing {len(empty_labels)} empty labels")
        for label_id in empty_labels:
            self._logger.warning(f"Removing empty label {label_id}")
            ApiException.raise_for_status(self._session.delete(f"{self._url}/labels/{label_id}"))

    def setup(self):
        colors = {
            DataLabels.ANIME: "green",
            DataLabels.MOVIE: "yellow",
            DataLabels.SERIES: "orange",
            DataLabels.CARTOON: "red",
            DataLabels.BOOKS: "purple",
            DataLabels.MANGA: "blue",
            DataLabels.COMPLETED: "black"
        }

        for name in DataLabels:
            color = colors.get(name, "black")
            self._ensure_trello_label(name.value, color)

    def _ensure_trello_label(self, name, color=None):
        try:
            label = self._labels.by_name(name)
        except KeyError:
            ret = self._session.post(
                f"{self._url}/boards/{self._board_id}/labels", params={
                    "name": name,
                    "color": color
                }
            )
            ApiException.raise_for_status(ret)
            label = Label(id=ret.json()["id"], name=name, color=color)
            self._labels.add(label)

        return label

    def _ensure_tags(self, names: typing.Iterable[str]):
        for name in names:
            yield self._ensure_trello_label(name, color=None)

    def _save_card_fields(self, is_new: bool, card: Card, scrapped: ScrappedData):
        card_fields = {}
        if is_new or card.title != scrapped.titles[0]:
            card.title = card_fields["name"] = scrapped.titles[0]

        scrapped_description = self._formatter.format_description(scrapped.url, scrapped)
        if is_new or self._formatter.format_description(card.source_url, card) != scrapped_description:
            card_fields["desc"] = scrapped_description
            card.alt_titles = scrapped.titles[1:]
            card.source_url = scrapped.url
            card.description = scrapped.description

        if is_new or scrapped.labels != card.labels or scrapped.tags != set(i.name for i in card.tags):
            scrapped_tags = set(self._ensure_tags(scrapped.tags))
            card_fields["idLabels"] = ",".join(
                self._formatter.format_labels(self._labels, scrapped.labels, scrapped_tags)
            )
            card.labels = scrapped.labels
            card.tags = scrapped_tags

        if card_fields:
            ApiException.raise_for_status(self._session.put(f"{self._url}/cards/{card.id}", params=card_fields))

    def _normalize_card_lists(self, scrapped_lists: typing.Sequence[List]):
        # normalize list sizes
        for scrapped_list in scrapped_lists:
            if len(scrapped_list.items) <= MAX_TRELLO_LIST_SIZE:
                yield scrapped_list
            else:
                for index, chunk in enumerate(more_itertools.chunked(scrapped_list.items, MAX_TRELLO_LIST_SIZE), start=1):
                    yield List(
                        name=f"{scrapped_list.name} #{index}",
                        items=chunk
                    )

    def _save_card_lists(self, card: Card, scrapped: ScrappedData):

        combined_parts = []
        card_part: List
        scrapped_part: List

        for card_part, scrapped_part in itertools.zip_longest(list(card.lists), self._normalize_card_lists(scrapped.parts)):
            if not card_part:
                ret = self._session.post(
                    f"{self._url}/cards/{card.id}/checklists", params={
                        "name": scrapped_part.name,
                        "pos ": "bottom"
                    }
                )
                ApiException.raise_for_status(ret)
                part = List(id=ret.json()["id"], name=scrapped_part.name)
                self._save_card_listitems(card, part, scrapped_part)
            elif not scrapped_part:
                self._session.delete(f"{self._url}/checklists/{card_part.id}")
                continue
            elif card_part.name != scrapped_part.name:
                ApiException.raise_for_status(self._session.put(
                    f"{self._url}/checklists/{card_part.id}", params={"name": scrapped_part.name}
                ))
                card_part.name = scrapped_part.name
                part = card_part
                self._save_card_listitems(card, card_part, scrapped_part)
            else:
                part = card_part
                self._save_card_listitems(card, card_part, scrapped_part)

            combined_parts.append(part)

        card.lists = combined_parts

    def _save_card_listitems(self, card: Card, card_list: List, scrapped_list: List):
        combined_items = []
        for pos, (card_item, scrapped_item) in enumerate(
                itertools.zip_longest(list(card_list.items), scrapped_list.items)
        ):
            if not card_item:
                ret = self._session.post(
                    f"{self._url}/checklists/{card_list.id}/checkItems", params={
                        "name": self._formatter.format_item(scrapped_item),
                        "pos": "bottom"
                    }
                )
                ApiException.raise_for_status(ret)
                scrapped_item.id = ret.json()["id"]
                combined_items.append(scrapped_item)
            elif not scrapped_item:
                ApiException.raise_for_status(self._session.delete(
                    f"{self._url}/checklists/{card_list.id}/checkItems/{card_item.id}"
                ))
                continue
            else:
                scrapped_name = self._formatter.format_item(scrapped_item)
                if scrapped_name != self._formatter.format_item(card_item):
                    ApiException.raise_for_status(self._session.put(
                        f"{self._url}/cards/{card.id}/checkItem/{card_item.id}", params={
                            "name": self._formatter.format_item(scrapped_item)
                        }
                    ))
                    scrapped_item.id = card_item.id
                    combined_items.append(scrapped_item)
                else:
                    combined_items.append(card_item)

        card_list.items = combined_items

    def _save_cover(self, card: Card, scrapped: ScrappedData):
        if scrapped.cover and not card.cover_id:
            self._logger.info("Setting cover ")
            cover_data, cover_mimetype = self._formatter.format_cover(scrapped.cover)
            ret = self._session.post(
                f"{self._url}/cards/{card.id}/attachments", params={
                    "name": "cover",
                    "mimeType": cover_mimetype,
                    "setCover": "true",
                }, files={"file": cover_data}
            )
            ApiException.raise_for_status(ret)
            card.cover_id = ret.json()["id"]
        if card.cover_id and not scrapped.cover:
            ret = self._session.delete(f"{self._url}/cards/{card.id}/attachments/{card.cover_id}")
            ApiException.raise_for_status(ret)
            card.cover_id = None

    def save(self, card: typing.Union[str, Card], scrapped: ScrappedData):
        is_new = False

        if isinstance(card, str):
            try:
                card = self.cards.by_id(card)
            except KeyError:
                is_new = True
                card = Card(
                    id=card,
                    source_url="",
                    title="",
                    version=VERSION
                )

        if is_new:
            try:
                existing_card = self.cards.by_source_url(scrapped.url)
            except KeyError:
                pass
            else:
                raise DuplicatedEntryException(f"Pending {scrapped.url} already exists as card {existing_card.id}")

        self._save_card_fields(is_new or card.version != VERSION, card, scrapped)
        if is_new:
            self.cards.add(card)
        self._save_card_lists(card, scrapped)
        self._save_cover(card, scrapped)

    def save_pending(self, pending: PendingCard, scrapped: ScrappedData):
        self.save(pending.id, scrapped)
