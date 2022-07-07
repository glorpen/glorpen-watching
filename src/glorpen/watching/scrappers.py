import html
import itertools
import json
import logging
import re
import typing
from datetime import datetime

import requests
from lxml.html import fromstring

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
        max_tries = 3
        for i in range(1, max_tries + 1):
            try:
                data = self.get_info(content)
                break
            except Exception as e:
                if max_tries > i:
                    self.logger.warning(f"Fetching failed with (try {i} of {max_tries})", exc_info=e)
                else:
                    raise e

        if not data:
            self.logger.error("No data found for url %r", url)
            return

        return data

    def supports_url(self, url) -> bool:
        raise NotImplementedError()

    def supports_labels(self, labels: set[DataLabels]) -> bool:
        raise NotImplementedError()

    def get_info(self, url) -> ScrappedData:
        raise NotImplementedError()

    def fetch_page(self, url, params=None):
        s = self.session.get(url, params=params or {}).content.decode()
        return fromstring(s)


class AnimePlanet(Scrapper):
    host = "https://www.anime-planet.com"
    re_host = re.compile('^https?://www.anime-planet.com/')

    def supports_url(self, url):
        return self.re_host.match(url) is not None

    def supports_labels(self, labels: set[DataLabels]) -> bool:
        return DataLabels.ANIME in labels

    def get_info(self, x):
        cover_url = str(x.xpath('//img[@class="screenshots"]/@src')[0])
        description = str(x.xpath('//meta[@property="og:description"]/@content')[0])

        if cover_url:
            if cover_url.startswith("/"):
                cover_url = self.host + cover_url
            cover_data = self.session.get(cover_url).content
        else:
            cover_data = None

        ended = "Watched" in x.xpath(
            '//div[contains(@class, "entrySynopsis")]//form//select[@class="changeStatus"]/option/text()'
        )
        episodes = int(x.xpath('//div[contains(@class, "entrySynopsis")]//form//select[@data-eps]/@data-eps')[0])
        names = [x.xpath('//h1[@itemprop="name"]/text()')[0]]
        genres = set(str(g).lower() for g in x.xpath('//meta[@property="video:tag"]/@content'))
        url = x.xpath('//link[@rel="canonical"]/@href')[0]

        alt_title = x.xpath('//h2[@class="aka"]/text()')
        if alt_title:
            alts = alt_title[0].strip()
            if alts.startswith("Alt title: "):
                names.append(alt_title[0][11:].strip())
            elif alts.startswith("Alt titles: "):
                names.extend(alt_title[0][12:].strip().split(", "))

        labels = {DataLabels.ANIME}

        if ended:
            labels.add(DataLabels.AIRING_ENDED)

        return ScrappedData(
            url=url,
            titles=names,
            cover=cover_data,
            description=description,
            labels=labels,
            tags=genres,
            parts=[List(name="Episodes", items=list(ListItem(number=e) for e in range(1, episodes + 1)))]
        )


class Imdb(Scrapper):
    host = "www.imdb.com"
    re_tid = re.compile('^.*/title/(tt[0-9]+).*$')
    re_url = re.compile('^https?://' + host + '/')

    def supports_labels(self, labels: set[DataLabels]) -> bool:
        return bool({DataLabels.MOVIE, DataLabels.SERIES, DataLabels.CARTOON}.intersection())

    def __init__(self):
        super(Imdb, self).__init__()
        self.session.headers.update(
            {
                "Accept-Language": "en-US,en;q=0.5"
            }
        )

    def supports_url(self, url):
        return self.re_url.match(url) is not None

    def _get_last_episode_year(self, episodes: typing.Iterable[List]):
        max_year = 0
        for season in episodes:
            for episode in season.items:
                if episode.date:
                    max_year = max(episode.date.year, max_year)
        return max_year

    def get_info(self, x):
        titles = []

        v = x.xpath('//script[@type="application/ld+json"]')[0]
        data = json.loads(v.text)

        v = x.xpath('//script[@type="application/json" and @id="__NEXT_DATA__"]')[0]
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
            labels.add(DataLabels.AIRING_ENDED)

        genres = set(i.lower() for i in data["genre"])

        if "animation" in genres:
            labels.add(DataLabels.CARTOON)
        elif data['@type'] == 'TVSeries':
            labels.add(DataLabels.SERIES)

        images = list(
            filter(
                None, (str(i) for i in x.xpath('//meta[@property="og:image"]/@content') if "imdb/images/logos" not in i)
            )
        )

        if images:
            cover = self.session.get(images[0]).content
        else:
            cover = None

        description = ("\n".join(
            i.strip() for i in
            x.xpath('//span[@data-testid="plot-xl"]/text()')
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


class NoScrapperAvailableException(Exception):
    pass


class ScrapperGuesser:
    re_http_link = re.compile('(?P<url>' + sre_http + ')')

    _scrappers = [
        AnimePlanet(),
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
            if card.labels:
                if not scrapper.supports_labels(card.labels):
                    continue

            for url in self.find_urls(card):
                if scrapper.supports_url(url):
                    return url, scrapper

        raise NoScrapperAvailableException("No scrapper supports this card")
