import html
import itertools
import json
import logging
import re
import time
import typing
from datetime import datetime

import more_itertools
import requests
from lxml.html import HtmlElement, fromstring

from glorpen.watching.model import DataLabels, Date, List, ListItem, PendingCard, ScrappedData

sre_http = '(?:(?:https?|ftp):\/\/)(?:\S+(?::\S*)?@)?(?:(?!(?:10|127)(?:\.\d{1,3}){3})(?!(?:169\.254|192\.168)(?:\.\d{1,3}){2})(?!172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})(?:[1-9]\d?|1\d\d|2[01]\d|22[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]-*)*[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]-*)*[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,}))\.?)(?::\d{2,5})?(?:[/?#]\S*)?'


def get_unique_list(iter):
    seen = set()
    return [x for x in iter if not (x in seen or seen.add(x))]


class Scrapper:

    def __init__(self):
        super(Scrapper, self).__init__()
        self.logger = logging.root.getChild(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update(
            {
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:77.0) Gecko/20100101 Firefox/77.0'
            }
        )

    def get(self, url: str):
        content = self.fetch_page(url)

        data = None
        max_tries = 4
        wait_seconds = 0
        for i in range(1, max_tries + 1):
            try:
                data = self.get_info(content)
                break
            except requests.HTTPError as e:
                if max_tries > i:
                    self.logger.warning(
                        f"Fetching failed on try {i} of {max_tries}, retrying after {wait_seconds}s. Error was:",
                        exc_info=e
                    )
                    if wait_seconds:
                        time.sleep(wait_seconds)
                        wait_seconds += 2
                else:
                    raise e

        if not data:
            self.logger.error("No data found for url %r", url)
            return

        return data

    def supports_url(self, url) -> bool:
        raise NotImplementedError()

    def get_info(self, doc) -> ScrappedData:
        raise NotImplementedError()

    def fetch_page(self, url, params=None) -> HtmlElement:
        s = self.session.get(url, params=params or {})
        s.raise_for_status()
        return fromstring(s.content.decode())


class AnimePlanet(Scrapper):
    host = "https://www.anime-planet.com"
    re_host = re.compile('^https?://www.anime-planet.com/')

    parts_name = {
        None: "Episodes",
        "volumes": "Volumes",
        "chapters": "Chapters"
    }

    def supports_url(self, url):
        return bool(self.re_host.match(url))

    def get_info(self, doc):
        cover_url = str(doc.xpath('//img[@class="screenshots"]/@src')[0])
        description = html.unescape(str(doc.xpath('//meta[@property="og:description"]/@content')[0]))

        meta_data = json.loads(doc.xpath('//script[@type="application/ld+json"]')[0].text)
        genres = set(str(g).lower() for g in meta_data["genre"])

        if cover_url:
            if cover_url.startswith("/"):
                cover_url = self.host + cover_url
            cover_data = self.session.get(cover_url).content
        else:
            cover_data = None

        available_statuses = set(map(str.lower, doc.xpath(
            '//div[contains(@class, "entrySynopsis")]//form//select[@class="changeStatus"]/option/text()'
        )))

        names = [doc.xpath('//h1[@itemprop="name"]/text()')[0]]
        url = doc.xpath('//link[@rel="canonical"]/@href')[0]

        alt_title = doc.xpath('//h2[@class="aka"]/text()')
        if alt_title:
            alts = alt_title[0].strip()
            if alts.startswith("Alt title: "):
                names.append(alt_title[0][11:].strip())
            elif alts.startswith("Alt titles: "):
                names.extend(alt_title[0][12:].strip().split(", "))

        labels = set()

        episode_control = more_itertools.first(filter(
            lambda x: int(x.attrib["data-eps"]) > 0,
            doc.xpath('//div[contains(@class, "entrySynopsis")]//form//select[@data-eps]')
        ))

        if meta_data["@type"] == "BookSeries":
            labels.add(DataLabels.MANGA)
            if "read" in available_statuses:
                labels.add(DataLabels.COMPLETED)
        else:
            labels.add(DataLabels.ANIME)
            if "watched" in available_statuses:
                labels.add(DataLabels.COMPLETED)

        if episode_control is None:
            parts = []
        else:
            parts = [List(name=self.parts_name[episode_control.attrib.get("name", None)], items=list(ListItem(number=e) for e in range(1, int(episode_control.attrib["data-eps"]) + 1)))]

        return ScrappedData(
            url=url,
            titles=names,
            cover=cover_data,
            description=description,
            labels=labels,
            tags=genres,
            parts=parts
        )


class Imdb(Scrapper):
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

    def _get_last_episode_year(self, episodes: typing.Iterable[List]):
        max_year = 0
        for season in episodes:
            for episode in season.items:
                if episode.date:
                    max_year = max(episode.date.year, max_year)
        return max_year

    def get_info(self, doc):
        titles = []

        v = doc.xpath('//script[@type="application/ld+json"]')[0]
        data = json.loads(v.text)

        v = doc.xpath('//script[@type="application/json" and @id="__NEXT_DATA__"]')[0]
        next_data = json.loads(v.text)
        release_year = next_data["props"]["pageProps"]["aboveTheFoldData"]["releaseYear"]
        if release_year["__typename"] != "YearRange":
            raise Exception(f"Unsupported release year range: {release_year}")

        url = "https://%s%s" % (self.host, data["url"])

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
            episodes = self.get_episodes_count(tid)
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

    def get_episodes_count(self, tid):
        self.logger.debug(f"Fetching episodes for {tid}")
        url = "https://%s/title/%s/episodes/_ajax" % (self.host, tid)

        ret = []

        x = self.fetch_page(url)
        seasons = x.xpath("//select[@id='bySeason']/option/@value")

        for season in seasons:
            self.logger.debug(f"Fetching season {season} for {tid}")
            x = self.fetch_page(url, params={"season": season})

            season_number = 0 if season == "-1" else int(season)

            episodes = []
            for lp_num, ep in enumerate(
                    x.xpath("//*[@itemtype='http://schema.org/TVSeason']//*[@itemprop='episodes']"), start=1
            ):
                ep_num = int(ep.xpath("./*[@itemprop='episodeNumber']/@content")[0])  # może być -1
                name = "".join(ep.xpath(".//*[@itemprop='name']/text()")).strip()
                air_date = "".join(ep.xpath(".//*[@class='airdate']/text()")).strip().replace('.', '')

                num = lp_num if ep_num == -1 else ep_num

                if name == 'Episode #%d.%d' % (int(season), num):
                    name = None

                if air_date:
                    try:
                        date = datetime.strptime(air_date, '%d %b %Y')
                        air_date = Date(date.year, date.month, date.day)
                    except:
                        try:
                            date = datetime.strptime(air_date, '%b %Y')
                            air_date = Date(date.year, date.month, None)
                        except:
                            date = datetime.strptime(air_date, '%Y')
                            air_date = Date(date.year, None, None)

                episodes.append(
                    ListItem(
                        name=name,
                        date=air_date or None,
                        number=ep_num
                    )
                )

            ret.append(List(name="Season %d" % season_number, items=episodes))

        return ret


class LibraryThing(Scrapper):
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
        title = doc.xpath("//h1/text()")[0].strip()
        author = doc.xpath("//h2/a/text()")[0].strip()

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
        AnimePlanet(),
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
