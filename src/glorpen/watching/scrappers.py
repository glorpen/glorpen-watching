import abc
import dataclasses
import enum
import functools
import html
import itertools
import json
import logging
import re
import textwrap
import time
import typing
import urllib.parse
from collections import OrderedDict
from datetime import datetime

import requests
import user_agent
from lxml.html import HtmlElement, fromstring

from glorpen.watching.model import DataLabels, Date, List, ListItem, PendingCard, ScrappedData

logger = logging.root.getChild(__name__)

sre_http = r'(?:(?:https?|ftp):\/\/)(?:\S+(?::\S*)?@)?(?:(?!(?:10|127)(?:\.\d{1,3}){3})(?!(?:169\.254|192\.168)(?:\.\d{1,3}){2})(?!172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})(?:[1-9]\d?|1\d\d|2[01]\d|22[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]-*)*[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]-*)*[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,}))\.?)(?::\d{2,5})?(?:[/?#]\S*)?'


def get_unique_list(iter):
    seen = set()
    return [x for x in iter if not (x in seen or seen.add(x))]


class Scrapper[S](abc.ABC):

    def __init__(self):
        super(Scrapper, self).__init__()
        self.logger = logging.root.getChild(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(
            {'User-Agent': user_agent.generate_user_agent()}
        )

    def get(self, url: str):
        content = self.fetch_page(url)
        try:
            data = self.get_info(content)
        except Exception as e:
            self.logger.exception(e)
            # with open("out.html", "wb") as f:
            #     f.write(tostring(content))
            raise e
        return data

    @abc.abstractmethod
    def supports_url(self, url) -> bool:
        raise NotImplementedError()

    @abc.abstractmethod
    def get_info(self, doc: S) -> ScrappedData:
        raise NotImplementedError()

    @abc.abstractmethod
    def fetch_page(self, url, params=None) -> S:
        raise NotImplementedError()


class HtmlScrapper(Scrapper[HtmlElement], abc.ABC):
    def fetch_page(self, url, params=None) -> HtmlElement:
        s = self.session.get(url, params=params or {})
        s.raise_for_status()
        return fromstring(s.content.decode())


def limit(max_requests_per_second: float):
    def inner(f: typing.Callable):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            now = time.time()
            if wrapper.last_request_time is not None:
                diff_seconds = (now - wrapper.last_request_time)
                seconds_to_wait = 1.0 / max_requests_per_second - diff_seconds
                if seconds_to_wait > 0:
                    logger.info(f"sleeping for {seconds_to_wait}")
                    time.sleep(seconds_to_wait)

            wrapper.last_request_time = now
            return f(*args, **kwargs)

        wrapper.last_request_time: typing.Optional[float] = time.time()

        return wrapper

    return inner


def http_retry(max_tries: int, codes=(500,)):
    def inner(f: typing.Callable):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            last_error = None
            for try_number in range(1, max_tries + 1):
                logger.debug(f"try {try_number} of {max_tries}")
                try:
                    return f(*args, **kwargs)
                except requests.exceptions.HTTPError as e:
                    response: requests.Response = e.response
                    last_error = e
                    if response.status_code not in codes:
                        raise e
            raise last_error

        return wrapper

    return inner


def remove_tags(text: str):
    return ''.join(fromstring(text).itertext())


class AniListFormat(enum.Enum):
    TV = "TV"
    TV_SHORT = "TV_SHORT"
    MOVIE = "MOVIE"
    SPECIAL = "SPECIAL"
    OVA = "OVA"
    ONA = "ONA"
    MUSIC = "MUSIC"
    MANGA = "MANGA"
    NOVEL = "NOVEL"
    ONE_SHOT = "ONE_SHOT"

    @classmethod
    def manga(cls):
        return {
            cls.MANGA,
            cls.ONE_SHOT
        }

    @classmethod
    def anime(cls):
        return {
            cls.TV,
            cls.TV_SHORT,
            cls.MOVIE,
            cls.SPECIAL,
            cls.OVA,
            cls.ONA,
        }


class AniListType(enum.Enum):
    ANIME = "ANIME"
    MANGA = "MANGA"


class AniListStatus(enum.Enum):
    FINISHED = "FINISHED"
    RELEASING = "RELEASING"
    NOT_YET_RELEASED = "NOT_YET_RELEASED"
    CANCELLED = "CANCELLED"
    HIATUS = "HIATUS"


class AniList(Scrapper[dict]):
    re_host = re.compile(r'^https?://anilist.co/[a-z]+/[0-9]+.*')
    max_requests_per_second = 15 / 60

    def get_query(self, anilist_id: int):
        query = textwrap.dedent(f"""\
        query ($id: Int) {{
            Media (id: $id) {{
                title {{
                    romaji
                    english
                    native
                }}
                status
                episodes
                type
                genres
                tags {{
                    name
                }}
                coverImage {{
                    extraLarge
                }}
                chapters
                volumes
                siteUrl
                description
            }}
        }}
        """)

        return {
            "query": query,
            "variables": {
                "id": anilist_id
            }
        }

    def get_title_query(self, title: str, type: AniListType, format: typing.Collection[AniListFormat]):
        query = textwrap.dedent(f"""\
        query ($title: String, $type: MediaType, $format: [MediaFormat]) {{
            Page (perPage: 10) {{
                media (search: $title, type: $type, format_in: $format) {{
                    id
                    title {{
                        romaji
                        english
                        native
                    }}
                }}
            }}
        }}
        """)

        return {
            "query": query,
            "variables": {
                "title": title,
                "type": type.value,
                "format": list(f.value for f in format)
            }
        }

    def supports_url(self, url):
        return bool(self.re_host.match(url)) or "anime-planet" in url

    _re_remove = re.compile(r'[^a-z0-9 ]+')
    _re_replace = re.compile(r'[:-]+')
    _re_fold_whitespace = re.compile(r'\s+')

    def get_id_from_anime_planet_url(self, url: str):
        title = urllib.parse.unquote(url.split("/")[-1]).replace("-", " ")
        is_manga = "/manga/" in url
        query = self.get_title_query(
            title=title,
            type=AniListType.MANGA if is_manga else AniListType.ANIME,
            format=AniListFormat.manga() if is_manga else AniListFormat.anime(),
        )

        s = self.session.post("https://graphql.anilist.co/", json=query)
        s.raise_for_status()
        for info in s.json()["data"]["Page"]["media"]:
            names = set()
            for api_title in info["title"].values():
                if not api_title:
                    continue
                api_title = self._re_replace.sub(' ', api_title.lower())
                api_title = self._re_remove.sub('', api_title)
                api_title = self._re_fold_whitespace.sub(' ', api_title).strip()
                if api_title:
                    names.add(api_title)
                    names.add(api_title.replace(" ", ''))

            if title in names or title.replace(" ", '') in names:
                return info["id"]

        raise Exception(f"AniList id was not found for {url}")

    @http_retry(3)
    @limit(max_requests_per_second=max_requests_per_second)
    def fetch_page(self, url, params=None) -> dict:
        if "anime-planet" in url:
            anilist_id = self.get_id_from_anime_planet_url(url)
        else:
            anilist_id = int(url.split("/")[4])

        query = self.get_query(anilist_id)
        s = self.session.post("https://graphql.anilist.co/", json=query)
        s.raise_for_status()
        return s.json()["data"]["Media"]

    def get_info(self, doc: dict):
        cover_url = doc["coverImage"]["extraLarge"]
        description = remove_tags(doc["description"])

        tags = set(g.lower() for g in doc["genres"])
        tags.update(g["name"].lower() for g in doc["tags"])

        if cover_url:
            cover_data = self.session.get(cover_url).content
        else:
            cover_data = None

        title_sort = ["english", "romaji", "native"]
        names = list(map(lambda x: x[1], sorted(doc["title"].items(), key=lambda x: title_sort.index(x[0]))))

        url = doc["siteUrl"]

        labels = set()

        entry_type = AniListType(doc["type"])
        if entry_type is AniListType.MANGA:
            labels.add(DataLabels.MANGA)
            if AniListStatus.FINISHED.value in doc["status"]:
                labels.add(DataLabels.COMPLETED)
        else:
            labels.add(DataLabels.ANIME)
            if AniListStatus.FINISHED.value in doc["status"]:
                labels.add(DataLabels.COMPLETED)

        if doc["chapters"] is not None:
            parts = [List(
                name="Chapters",
                items=list(ListItem(number=e + 1) for e in range(0, doc["chapters"]))
            )]
        elif doc["episodes"] is not None:
            parts = [List(
                name="Episodes",
                items=list(ListItem(number=e + 1) for e in range(0, doc["episodes"]))
            )]
        elif doc["volumes"] is not None:
            parts = [List(
                name="Volumes",
                items=list(ListItem(number=e + 1) for e in range(0, doc["volumes"]))
            )]
        else:
            parts = []

        return ScrappedData(
            url=url,
            titles=names,
            cover=cover_data,
            description=description,
            labels=labels,
            tags=tags,
            parts=parts
        )


@dataclasses.dataclass
class ImdbEpisode:
    season: str
    episode: str
    name: str | None
    date: Date


class Imdb(HtmlScrapper):
    host = "www.imdb.com"
    re_tid = re.compile('^.*/title/(tt[0-9]+).*$')
    re_url = re.compile('^https?://' + host + '/')

    def __init__(self):
        super(Imdb, self).__init__()
        self.session.headers.update(
            {
                "Accept-Language": "en-US,en;q=0.5"
            }
        )

    def supports_url(self, url):
        return bool(self.re_url.match(url))

    @http_retry(3)
    def fetch_page(self, url, params=None) -> HtmlElement:
        return super(Imdb, self).fetch_page(url, params=params)

    @classmethod
    def _get_last_episode_year(cls, episodes: typing.Iterable[List]):
        max_year = 0
        for season in episodes:
            for episode in season.items:
                if episode.date:
                    max_year = max(episode.date.year, max_year)
        return max_year

    def parse_doc_data(self, doc: HtmlElement, type: str = "application/json", id: str = None) -> dict:
        # with open("out.html", "wb") as f:
        #     f.write(tostring(doc))
        if id is None:
            q = f'//script[@type="{type}"]'
        else:
            q = f'//script[@type="{type}" and @id="{id}"]'
        return json.loads(doc.xpath(q)[0].text)

    def get_info(self, doc):
        titles = []

        data = self.parse_doc_data(doc, type="application/ld+json")

        next_data = self.parse_doc_data(doc, id="__NEXT_DATA__")
        release_year = next_data["props"]["pageProps"]["aboveTheFoldData"]["releaseYear"]
        if release_year["__typename"] != "YearRange":
            raise Exception(f"Unsupported release year range: {release_year}")

        url = data["url"]

        if "alternateName" in data:
            titles.append(html.unescape(data["alternateName"]))
        titles.append(html.unescape(data["name"]))

        labels = set()
        ended = False

        if data['@type'] == "Movie":
            episodes = []
            labels.add(DataLabels.MOVIE)
            ended = True
        else:
            tid = self.re_tid.match(url).group(1)
            episodes = list(self.get_episodes(tid))
            if release_year["endYear"] is not None:
                ended = release_year["endYear"] == self._get_last_episode_year(episodes) and datetime.now().year > \
                        release_year["endYear"]

        if ended:
            labels.add(DataLabels.COMPLETED)

        genres = set(i.lower() for i in data["genre"])

        if "animation" in genres:
            labels.add(DataLabels.CARTOON)
        elif data['@type'] == 'TVSeries':
            labels.add(DataLabels.SERIES)

        images = list(
            filter(
                None,
                (str(i) for i in doc.xpath('//meta[@property="og:image"]/@content') if "imdb/images/logos" not in i)
            )
        )

        if images:
            cover = self.session.get(images[0]).content
        else:
            cover = None

        description = ("\n".join(
            i.strip() for i in
            doc.xpath('//span[@data-testid="plot-xl"]/text()')
        )).strip()

        return ScrappedData(
            url=url,
            titles=get_unique_list(titles),
            parts=episodes,
            tags=genres,
            cover=cover,
            description=description,
            labels=labels
        )

    def get_episodes_query(self, tid: str, end_cursor: str):
        return {
            "variables": json.dumps({
                "after": end_cursor,
                "const": tid,
                "first": 100,
                "locale": "en-US",
                "originalTitleText": False,
                "returnUrl": "https://www.imdb.com/close_me",
                "sort": {"by": "EPISODE_THEN_RELEASE", "order": "ASC"}
            }),
            "operationName": "TitleEpisodesSubPagePagination",
            "extensions": json.dumps({"persistedQuery": {
                "sha256Hash": "e5b755e1254e3bc3a36b34aff729b1d107a63263dec628a8f59935c9e778c70e", "version": 1}})
        }

    def get_more_episodes(self, tid: str, end_cursor: str):
        while True:
            r = self.session.get(
                "https://caching.graphql.imdb.com/",
                params=urllib.parse.urlencode(self.get_episodes_query(tid=tid, end_cursor=end_cursor),
                                              quote_via=urllib.parse.quote),
                headers={
                    "Accept": "application/graphql+json, application/json",
                    "Content-Type": "application/json"
                }
            )
            r.raise_for_status()

            episodes = r.json()["data"]["title"]["episodes"]["episodes"]
            if not episodes:
                break
            for i in episodes["edges"]:
                item = i["node"]
                ep_info = item["series"]["displayableEpisodeNumber"]
                yield ImdbEpisode(
                    name=item["titleText"]["text"],
                    date=Date(
                        year=item["releaseDate"]["year"],
                        month=item["releaseDate"]["month"],
                        day=item["releaseDate"]["day"],
                    ) if item["releaseDate"] else None,
                    season=ep_info["displayableSeason"]["displayableProperty"]["value"]["plainText"],
                    episode=ep_info["episodeNumber"]["displayableProperty"]["value"]["plainText"]
                )
            if not episodes["pageInfo"]["hasNextPage"]:
                break
            end_cursor = episodes["pageInfo"]["endCursor"]

    def get_episodes(self, tid: str) -> list[List]:
        episodes = OrderedDict()

        for ep in self.iter_episodes(tid):
            name = ep.name
            if ep.name == f'Episode #{ep.season}.{ep.episode}':
                name = None

            if ep.season not in episodes:
                episodes[ep.season] = []

            episodes[ep.season].append(
                ListItem(
                    name=name,
                    date=ep.date,
                    number=None if ep.episode == "Unknown" else int(ep.episode)
                )
            )

        ret = []
        for season, eps in episodes.items():
            ret.append(List(name=f"Season {season}", items=eps))

        return ret

    def iter_episodes(self, tid) -> typing.Iterable[ImdbEpisode]:
        self.logger.debug(f"Fetching episodes for {tid}")

        url = f"https://{self.host}/title/{tid}/episodes/?season=1"
        data = self.parse_doc_data(self.fetch_page(url), id="__NEXT_DATA__")

        data_section = data["props"]["pageProps"]["contentData"]["section"]
        data_episodes = data_section["episodes"]

        for item in data_episodes["items"]:
            yield ImdbEpisode(
                name=item["titleText"],
                date=Date(
                    year=item["releaseDate"]["year"],
                    month=item["releaseDate"]["month"],
                    day=item["releaseDate"]["day"],
                ) if item["releaseDate"] else None,
                episode=item["episode"],
                season="1",
            )

        end_cursor = data_episodes["endCursor"]
        yield from self.get_more_episodes(tid=tid, end_cursor=end_cursor)


class LibraryThing(HtmlScrapper):
    re_url = re.compile('^https?://(?:www.)?librarything.com/')
    re_tag_cloud = re.compile(r'ajax_work_makeworkCloud\((\d+), (\d+)\)')
    re_font_size = re.compile(r'\d(?:.\d)?')

    ignored_tags = {
        "own", "read", "1001", "1001 books", "ebook", "to-read", "unread"
    }

    def supports_url(self, url) -> bool:
        return bool(self.re_url.match(url))

    def fetch_tags(self, work: int, check: int):
        req = self.session.post(f"https://www.librarything.com/ajax_work_makeworkCloud.php?work={work}&check={check}")
        req.raise_for_status()
        return self.select_tags(fromstring(req.content))

    def select_tags(self, doc_tags):
        ret = {}

        for tag_container in doc_tags.xpath("//div[@class='tags tagcloud_tags']/span[@class='tag']"):
            tag_value = float(self.re_font_size.search(tag_container.attrib["style"]).group(0))
            tag_name = tag_container.xpath(".//a/text()")[0].lower()

            ret[tag_name] = tag_value

        return ret

    def filter_tags(self, tags: dict[str, float]):
        for name, value in tags.items():
            if value < 1:
                continue
            if name in self.ignored_tags:
                continue
            yield name

    def get_info(self, doc: HtmlElement) -> ScrappedData:
        x_summary = doc.xpath("//tr[contains(@class, 'wslsummary')]//div[@class='showmore']")
        if x_summary:
            x_summary = x_summary[0]
            description = "".join(filter(None, x_summary.xpath("./text()") + x_summary.xpath("./u/text()")))
        else:
            description = None

        url = doc.xpath("/html/head/link[@rel='canonical']/@href")[0]
        work_id = int(url.split("/")[-1])

        tag_js = doc.xpath("/html/body/script[contains(text(), 'ajax_work_makeworkCloud')][1]/text()")
        if tag_js:
            m = self.re_tag_cloud.search(tag_js[0])
            tags = set(self.filter_tags(self.fetch_tags(m.group(1), m.group(2))))
        else:
            tags = set(self.filter_tags(self.select_tags(doc)))

        # take last srcset url, it probably is the biggest
        # also be lazy and assume that there is no x10 srcset
        cover_url = doc.xpath("//div[@id='maincover']/img/@srcset")[0].split(", ")[-1][0:-3]
        title = doc.xpath("//div[contains(@class, 'content')]//h1/text()")[0].strip()
        author = doc.xpath("//div[contains(@class, 'content')]//h2/a/text()")[0].strip()

        return ScrappedData(
            description=description,
            titles=[
                f'"{title}", {author}'
            ],
            labels={DataLabels.BOOKS, DataLabels.COMPLETED},
            cover=self.session.get(cover_url).content if cover_url else None,
            parts=[],
            tags=tags,
            url=url
        )


class NoScrapperAvailableException(Exception):
    pass


class ScrapperGuesser:
    re_http_link = re.compile('(?P<url>' + sre_http + ')')

    _scrappers = [
        AniList(),
        LibraryThing(),
        Imdb()
    ]

    def find_urls(self, card: PendingCard):
        i = [
            self.re_http_link.finditer(card.name),
            self.re_http_link.finditer(card.description)
        ]

        for m in itertools.chain(*i):
            yield m.groupdict()["url"]

    def get_for_url(self, url: str):
        for scrapper in self._scrappers:
            if scrapper.supports_url(url):
                return scrapper

        raise NoScrapperAvailableException("Not supported url")

    def get_for_pending(self, card: PendingCard):
        for scrapper in self._scrappers:
            for url in self.find_urls(card):
                if scrapper.supports_url(url):
                    return url, scrapper

        raise NoScrapperAvailableException("No scrapper supports this card")
