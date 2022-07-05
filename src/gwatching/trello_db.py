import abc
import datetime
import functools
import io
import logging
import mimetypes
import re
import typing

import PIL.Image
from requests_oauthlib.oauth1_session import OAuth1Session

from gwatching.model import Card, DataLabels, DuplicatedEntryException, Label, List, ListItem, \
    ParsedRawDescription, \
    PendingCard, ScrappedData

api_host = 'api.trello.com'
api_version = 1
app_name = "Grello"

MODE_READ = 1 << 0
MODE_WRITE = 1 << 1
MODE_ACCOUNT = 1 << 2


class NotAuthorizedException(Exception):
    pass


class TokenFetcher:
    def __init__(self, app_key: str, app_secret: str, token_expiration='never',
                 token_mode=MODE_READ | MODE_WRITE | MODE_ACCOUNT):
        pass


class DescriptionParser(abc.ABC):
    def parse_description(self, description: str) -> ParsedRawDescription:
        raise NotImplementedError()

    def parse_checklist_item(self, item: dict) -> ListItem:
        raise NotImplementedError()


class DescriptionParserV0(DescriptionParser):
    _re_checklist_item = re.compile(r"\*\*(?P<number>-?\d+)\*\*:\s*(:?\*(?P<name>.*)\*)?\s*(:?\[(?P<date>[\d-]+)\])?")

    def parse_checklist_item(self, item: dict) -> ListItem:
        if item["name"].startswith("*"):
            m = self._re_checklist_item.match(item["name"])
            if not m:
                raise Exception(f"Could not parse {item['name']}")
            parts = m.groupdict()
            return ListItem(
                id=item["id"],
                number=int(parts["number"]),
                date=datetime.date(*(int(i) for i in parts["date"].split("-"))) if parts["date"] else None,
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
        position = 0
        alt_titles = []
        if lines[0].startswith("> Alt title:"):
            alt_titles.append(lines[0][14:-1])
            position += 2
        elif lines[0].startswith("> Alt titles:"):
            for line in lines[1:]:
                if line.startswith("> "):
                    alt_titles.append(line[3:-1])
                    position += 1
                else:
                    break
            position += 1

        snip_index = lines.index("---")
        description = "\n".join(lines[position:snip_index]).strip() or None
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
        labels = set()
        for name in DataLabels:
            try:
                self.by_name(name)
            except KeyError:
                continue
            labels.add(name)
        return labels

    def tags(self) -> set[Label]:
        data_labels = set(label.value for label in DataLabels)
        tags = set()
        for key, value in self._by_names.items():
            if key in data_labels:
                continue
            tags.add(value)
        return tags

    def __len__(self):
        return len(self._by_id)


class CardBag:
    def __init__(self):
        super(CardBag, self).__init__()
        self._by_id: dict[str, Card] = {}
        self._by_source_url: dict[str, Card] = {}
        self.pending: list[PendingCard] = []

    def add(self, card: Card):
        if card.id in self._by_id:
            raise DuplicatedEntryException(f"Card with id {card.id} already exist")
        if card.source_url in self._by_source_url:
            raise DuplicatedEntryException(f"Card with source url {card.source_url} already exist")

        self._by_id[card.id] = self._by_source_url[card.source_url] = card

    def add_pending(self, card: PendingCard):
        self.pending.append(card)


class DataFormatter:
    _re_card_source_url_host = re.compile(r'^[a-z]+://([^/]+).*$')

    def format_cover(self, card: ScrappedData):
        with PIL.Image.open(io.BytesIO(card.cover)) as im:
            if len(card.cover) > (1 << 21):
                cover_data = io.BytesIO()
                im.save(cover_data, "JPEG")
                mime_type = "image/jpeg"
            else:
                cover_data = card.cover
                mime_type = mimetypes.guess_type("file.%s" % im.format)[0]

        return cover_data, mime_type

    def format_description(self, card: Card):
        lines = []

        if card.alt_titles:
            if len(card.alt_titles) > 1:
                lines.append("> Alt titles:\n%s\n\n" % ("\n".join("> *%s*" % i for i in card.alt_titles)))
            else:
                lines.append(f"> Alt title: *{card.alt_titles[0]}*\n\n")

        if card.description:
            lines.extend([card.description, ""])

        source_name = self._re_card_source_url_host.match(card.source_url).group(1)
        if len(source_name) > 46:
            source_name = source_name[:46] + "..."
        else:
            source_name = source_name

        lines.extend(
            [
                "---",
                "",
                f"Source: [{source_name}]({card.source_url})",
                "Version: 0.0.3"
            ]
        )


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

    def get_parser(self, description: str) -> DescriptionParser:
        return self._parsers[self.get_version(description)]


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
    def _cards(self):
        cards = CardBag()
        checklists = dict()

        for checklist in self._session.get(f"{self._url}/boards/{self._board_id}/checklists").json():
            checklists[checklist["id"]] = checklist

        for card in self._get_api_cards():
            try:
                labels = LabelBag(self._labels.by_id(i) for i in card["idLabels"])

                if DataLabels.BOOKS in labels.data():
                    continue

                try:
                    parser = self._version_detector.get_parser(card["desc"])
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

                parsed_description = parser.parse_description(card["desc"])

                if card['cover']:
                    cover_url = f"https://trello.com/1/cards/{card['id']}/attachments/{card['cover']['idAttachment']}/download/file"
                else:
                    cover_url = None

                card_checklists = []

                for checklist_id in card["idChecklists"]:
                    checklist = checklists[checklist_id]
                    card_checklists.append(
                        List(
                            name=checklist["name"],
                            id=checklist["id"],
                            items=list(parser.parse_checklist_item(item) for item in checklist["checkItems"])
                        )
                    )

                card_model = Card(
                    id=card["id"],
                    lists=card_checklists,
                    cover_url=cover_url,
                    title=card["name"],
                    tags=labels.tags(),
                    labels=labels.data(),
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

        # TODO: don't touch colored labels?

        for label in self._get_api_labels():
            name = label["name"]
            id = label["id"]
            if name in known_labels:
                duplicated_labels.setdefault(name, []).append(id)
            else:
                known_labels[name] = id

        print(f"Found {len(duplicated_labels)} duplicated labels")

        for card in self._get_api_cards():
            for label in card["labels"]:
                name = label["name"]
                id = label["id"]
                if name in duplicated_labels and id in duplicated_labels[name]:
                    self._logger.warning(f'Replacing label "{name}" on card {card["id"]} / {card["name"]}')
                    # add new label
                    self._session.post(
                        f"{self._url}/cards/{card['id']}/idLabels", params={
                            "value": known_labels[name]
                        }
                    ).raise_for_status()
                    # remove duplicated label
                    self._session.delete(f"{self._url}/cards/{card['id']}/idLabels/{id}").raise_for_status()
                    used_labels.add(known_labels[name])
                else:
                    used_labels.add(id)

        if duplicated_labels:
            print(f"Removing duplicated labels")
            for labels in duplicated_labels.values():
                for label_id in labels:
                    self._logger.warning(f"Removing duplicated label {label_id}")
                    self._session.delete(f"{self._url}/labels/{label_id}").raise_for_status()

        unused_labels = set(known_labels.values()).difference(used_labels)
        print(f"Found {len(unused_labels)} unused labels")
        for label_id in unused_labels:
            self._logger.warning(f"Removing unused label {label_id}")
            self._session.delete(f"{self._url}/labels/{label_id}").raise_for_status()

    def update_card(self, card: Card, scrapped: ScrappedData):
        pass

    def save_pending_card(self, pending: PendingCard, scrapped: ScrappedData):
        pass

    def get_pending_cards(self):
        return self._cards.pending
