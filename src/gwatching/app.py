'''
Created on 29.12.2016

@author: glorpen
'''
import logging
from grello.objects import Label, Notification
import re
import itertools
import requests
from lxml.html import fromstring
import collections
from datetime import datetime
import argparse
from PIL import Image
import io
import mimetypes
from grello.connection import Api
from grello.ui import ConsoleUi

sre_http = '(?:(?:https?|ftp):\/\/)(?:\S+(?::\S*)?@)?(?:(?!(?:10|127)(?:\.\d{1,3}){3})(?!(?:169\.254|192\.168)(?:\.\d{1,3}){2})(?!172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})(?:[1-9]\d?|1\d\d|2[01]\d|22[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]-*)*[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]-*)*[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,}))\.?)(?::\d{2,5})?(?:[/?#]\S*)?'

class Scrapper(object):
    headers = {'User-Agent': 'Mozilla/5.0 (DirectX; Windows 10; rv:38.0) Gecko/20100101 Firefox/38.0'}
    
    re_http_link = re.compile('(?P<url>'+sre_http+')')
    re_normalized_link = re.compile('^\s*Source\s*:\s+\[[^\]]+\]\((?P<url>'+sre_http+')\)\s*$', re.MULTILINE)
    
    def compress_image(self, image_data):
        ret = io.BytesIO()
        with Image.open(io.BytesIO(image_data)) as im:
            im.save(ret, "JPEG")
        ret.seek(0)
        return ret

    def detect_mime_type(self, image_data):
        with Image.open(io.BytesIO(image_data)) as im:
            return mimetypes.guess_type("file.%s" % im.format)[0]

    def __init__(self):
        super(Scrapper, self).__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()
    
    def find_urls(self, card):
        i = [
            self.re_normalized_link.finditer(card.description),
            self.re_http_link.finditer(card.name),
            self.re_http_link.finditer(card.description)
        ]
        
        for m in itertools.chain(*i):
            yield m.groupdict()["url"]
    
    def find_data_links(self, card):
        for url in self.find_urls(card):
            if self.is_known_url(url):
                return url
    
    def update(self, board, card, aired_label):
        self.logger.debug("Updating card %r", card)
        
        url = self.find_data_links(card)
        if not url:
            self.logger.warning("No url found for card %r", card)
            return
        
        self.logger.debug("Using url %r", url)
        
        content = self.fetch_page(url)
        data = self.get_info(content)
        
        if not data:
            self.logger.error("No data found for url %r", url)
            return
        
        self.update_card(board, card, data, aired_label)
    
    def create_description(self, text, url):
        if len(url) > 46:
            name = url[:46] + "..."
        else:
            name = url
        
        link = "Source: [%s](%s)" % (name, url)
        return "%s\n\n---\n\n%s" % (text, link) 
    
    def is_known_url(self, url):
        raise NotImplementedError()
    
    def get_info(self, url):
        raise NotImplementedError()

    def update_genres(self, board, card, genres):
        current_labels = dict((l.name, l) for l in card.labels if l.color is Label.NO_COLOR)
        new_keys, _existing_items, deleted_items = self._diff(current_labels, genres)
        
        for l in deleted_items.values():
            self.logger.debug("Removing label %r from %r", l, card)
            card.labels.remove(l)
        
        current_board_labels = dict((l.name, l) for l in board.labels if l.color is Label.NO_COLOR)
        labels_to_add, labels_existing, _dummy2 = self._diff(current_board_labels, new_keys)
        for name in labels_to_add:
            self.logger.debug("Adding label %r to %r", name, board)
            labels_existing[name] = board.labels.add(name=name, color=Label.NO_COLOR)
            
        for label in labels_existing.values():
            self.logger.debug("Adding label %r to %r", label, card)
            card.labels.add(label)

#     def clean_board_labels(self, board):
#         for l in list(board.labels):
#             if l.color is Label.NO_COLOR and l.uses == 0:
#                 board.labels.remove(l)
    
    def _diff(self, current_items, target_keys):
        
        existing_items = {}
        deleted_items = {}
        new_keys = []
        
        for k,v in current_items.items():
            if k in target_keys:
                existing_items[k] = v
            else:
                deleted_items[k] = v
        
        for name in target_keys:
            if name not in existing_items:
                new_keys.append(name)
        
        return new_keys, existing_items, deleted_items
    
    def update_seasons(self, card, seasons):
        current_checklists = dict((i.name, i) for i in card.checklists)
        
        new_keys, existing_items, deleted_items = self._diff(current_checklists, tuple(seasons.keys()))
        
        for i in deleted_items.values():
            card.checklists.remove(i)
        
        for i in new_keys:
            existing_items[i] = card.checklists.add(name=i)
        
        for name, checklist in existing_items.items():
            items = iter(seasons.get(name))
            
            for ci in list(checklist.items):
                v = next(items, None)
                
                if v is None:
                    checklist.items.remove(ci)
                else:
                    ci.name = v
            
            while True:
                v = next(items, None)
                if v is None:
                    break
                
                checklist.items.add(name=v)
    
    def update_card(self, board, card, data, aired_label):
        card.name = data["name"]
        card.description = self.create_description(data["description"], data["url"])
        
        if not card.cover and data["cover"]:

            cover_data = self.session.get(data["cover"]).content

            if len(cover_data) > (1<<21):
                cover_data = self.compress_image(cover_data)
                mime_type = "image/jpeg"
            else:
                mime_type = self.detect_mime_type(cover_data)

            a = card.attachments.add(name="cover", file=cover_data, mime_type=mime_type)
            card.cover = a
        
        self.update_seasons(card, data["episodes"])
        
        self.update_genres(board, card, data["genres"])
        #self.clean_board_labels(board)
        
        if data["ended"]:
            card.labels.add(aired_label)
    
    def fetch_page(self, url, params = {}):
        s = self.session.get(url, headers=self.headers, params=params).content.decode()
        return fromstring(s)
    
class AnimePlanet(Scrapper):
    
    host = "https://www.anime-planet.com"
    re_host = re.compile('^https?://www.anime-planet.com/')
    
    def is_known_url(self, url):
        return self.re_host.match(url) is not None
    
    def get_info(self, x):
        try:
            cover = self.host+str(x.xpath('//img[@class="screenshots"]/@src')[0])
            description = "\n".join(x.xpath('//div[@itemprop="description"]/p/text()'))
        except IndexError:
            return None
        
        ended = "Watched" in x.xpath('//div[contains(@class, "entrySynopsis")]/following-sibling::form//select[@class="changeStatus"]/option/text()')
        episodes = int(x.xpath('//div[contains(@class, "entrySynopsis")]/following-sibling::form//select[@data-eps]/@data-eps')[0])
        name = x.xpath('//h1[@itemprop="name"]/text()')[0]
        genres = tuple(str(g).lower() for g in x.xpath('//li[@itemprop="genre"]/a/text()'))
        url = x.xpath('//link[@rel="canonical"]/@href')[0]
        
        return {
            "url": url,
            "cover": cover,
            "description": description,
            "ended": ended,
            "episodes": {"Episodes": list(("%.2d" % e) for e in range(1, episodes+1))},
            "name": name,
            "genres": genres
        }

class Imdb(Scrapper):
    
    host = "http://www.imdb.com"
    re_cover_url = re.compile('^(.*?/[0-9a-zA-Z]+@?)\..*$')
    re_tid = re.compile('^.*/title/(tt[0-9]+).*$')
    
    def __init__(self, is_movie=False):
        super(Imdb, self).__init__()
        self.is_movie = is_movie
    
    def is_known_url(self, url):
        return url.startswith(self.host+"/")
    
    def get_info(self, x):
        title = None
        
        url = x.xpath('//meta[@property="og:url"]/@content')[0]
        
        org_title_els = x.xpath('//div[@class="originalTitle"]/text()')
        if org_title_els:
            title = org_title_els[0]
        
        if not title:
            title = x.xpath('//h1[@itemprop="name"]/text()')[0].strip()
        
        if self.is_movie:
            episodes = {}
            ended = True
        else:
            tid = self.re_tid.match(url).group(1)
            episodes, ended = self.get_episodes_count(tid)
        
        genres = [i.strip().lower() for i in x.xpath('//span[@itemprop="genre"]/text()')]
        
        images = list(filter(None, (self.re_cover_url.match(i) for i in x.xpath('//meta[@property="og:image"]/@content') if "imdb/images/logos" not in i)))
        
        if images:
            image = images[0]
            cover = image.group(1)
        else:
            cover = None
        
        description = ("\n".join(i.strip() for i in x.xpath('//div[@itemprop="description"]/text()'))).strip()
        
        return {
            "url": url,
            "name": title,
            "episodes": episodes,
            "genres": genres,
            "cover": cover,
            "description": description,
            "ended": ended
        }

    def get_episodes_count(self, tid):
        url = "%s/title/%s/episodes/_ajax" % (self.host, tid)
        
        ret = collections.OrderedDict()
        ended = True
        now = datetime.utcnow()
        
        x = self.fetch_page(url)
        seasons = x.xpath("//select[@id='bySeason']/option/@value")
        
        for season in seasons:
            x = self.fetch_page(url, params={"season":season})
            
            season_number = 0 if season == "-1" else int(season)
            
            episodes = []
            for lp_num, ep in enumerate(x.xpath("//*[@itemtype='http://schema.org/TVSeason']//*[@itemprop='episodes']"), start=1):
                ep_num = int(ep.xpath("./*[@itemprop='episodeNumber']/@content")[0]) # może być -1
                name = "".join(ep.xpath(".//*[@itemprop='name']/text()")).strip()
                air_date = "".join(ep.xpath(".//*[@class='airdate']/text()")).strip().replace('.', '')
                
                num = lp_num if ep_num == -1 else ep_num
                
                if name == 'Episode #%d.%d' % (int(season), num):
                    name = None
                
                if air_date:
                    try:
                        date = datetime.strptime(air_date, '%d %b %Y')
                        
                        if date > now:
                            ended = False
                         
                        air_date = date.strftime("%Y-%m-%d")
                    except:
                        try:
                            date = datetime.strptime(air_date, '%b %Y')
                            if date.year >= now.year:
                                ended = False
                            air_date = date.strftime("%Y-%m-??")
                        except:
                            date = datetime.strptime(air_date, '%Y')
                            if date.year >= now.year:
                                ended = False
                            air_date = date.strftime("%Y-??-??")
                else:
                    ended = False
                
                ep_label = "**%.2d**:" % ep_num
                if name:
                    ep_label += " *%s*" % name
                if air_date:
                    ep_label += " [%s]" % (air_date,)
                
                episodes.append(ep_label)
            
            ret["Season %d" % season_number] = episodes
            
        return ret, ended

class Ui(ConsoleUi):
    def load_keys(self):
        return ('xxx', 'xxx')


class TrelloShowUpdater(object):
    
    api = None
    airing_ended_label = "airing ended"
    
    def __init__(self):
        super(TrelloShowUpdater, self).__init__()
        
        self.logger = logging.getLogger(self.__class__.__name__)
        self.updaters = {}
    
    def get_api(self):
        if self.api is None:
            a = Api(
                "aaa",
                "bbb"
            )
            a.assure_token("xxx")
            
            self.api = a
        
        return self.api
    
    def register_updater(self, label_name, updater):
        self.updaters[label_name] = updater
    
#     def clean(self, board):
#         for l in list(board.labels):
#             if l.uses == 0 and l.color is Label.NO_COLOR:
#                 print("Removing label %r" % l.name)
#                 board.labels.remove(l)
    
    def update_card(self, board, l, c, aired_label, allowed_labels):
        print("Checking card %r/%r" % (l.name, c.name))
        label_names = set(l.name for l in c.labels)

        if self.airing_ended_label in label_names:
            self.logger.info("Skipping already aired show on card %r", c)
            return

        found_labels = label_names.intersection(self.updaters.keys())
        if found_labels:
            if found_labels.intersection(allowed_labels):
                print("Updating")
                self.updaters.get(found_labels.pop()).update(board, c, aired_label)
                return True
            else:
                return False
        else:
            self.logger.info("No supported labels for card %r", c.id)
            return None

    def update(self, board_id, short=False, allowed_labels=None):
        api = self.get_api()
        board = api.get_board(board_id)
        board.subscribed = True

        allowed_labels = set(allowed_labels or self.updaters.keys())

        me = api.get_me()

        cards_checked = []
        
        aired_label = None
        for label in board.labels:
            if label.name == self.airing_ended_label:
                aired_label = label
        if not aired_label:
            aired_label = board.labels.add(name=self.airing_ended_label, color=Label.BLACK)
        
        for n in me.notifications:
            if not n.unread:
                continue

            if n.type in (Notification.CREATED_CARD, Notification.CHANGED_CARD):
                if n.board and n.board is board and n.card:
                    if self.update_card(board, n.card.list, n.card, aired_label, allowed_labels) is False:
                        continue
            else:
                print("Skipping notification of type %r" % n.type)

            n.read()

        if short:
            return

        for l in board.lists:
            print("Checking list %r" % l.name)
            for c in l.cards:
#                 if c.id != "5a4a02bc30937b60be164eba":
#                     continue
                if c.id in cards_checked:
                    continue
                self.update_card(board, l, c, aired_label, allowed_labels)

        #self.clean(board)
        
if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--anime', '-a', action='append_const', const="anime", dest="labels")
    parser.add_argument('--movie', '-m', action='append_const', const="movie", dest="labels")
    parser.add_argument('--series', '-s', action='append_const', const="series", dest="labels")
    parser.add_argument('--cartoon', '-c', action='append_const', const="cartoon", dest="labels")

    parser.add_argument('--short', action='store_true')

    parser.add_argument('--verbose', '-v', action='count', default=0)

    args = parser.parse_args()

    log_levels = [
        logging.ERROR,
        logging.WARN,
        logging.INFO,
        logging.DEBUG
    ]

    log_level = log_levels[min(len(log_levels), args.verbose)]
    logging.basicConfig(level=log_level)

    
    t = TrelloShowUpdater()
    t.register_updater("anime", AnimePlanet())
    t.register_updater("movie", Imdb(True))
    t.register_updater("series", Imdb(False))
    t.register_updater("cartoon", Imdb(False))

    labels = args.labels or None
    
    #t.update('aaa', short=args.short, allowed_labels=labels) #4CnhxrXj
    t.update('aaa', short=args.short, allowed_labels=labels)
    #t.update('aaa', short=args.short, allowed_labels=labels)

