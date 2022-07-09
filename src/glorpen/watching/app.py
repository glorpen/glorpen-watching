import argparse
import logging
import sys
import textwrap
import typing

import tqdm
from tqdm.contrib import DummyTqdmFile
from tqdm.contrib.logging import logging_redirect_tqdm

from glorpen.watching.config import load_config
from glorpen.watching.model import Card, DataLabels, PendingCard
from glorpen.watching.scrappers import NoScrapperAvailableException, ScrapperGuesser
from glorpen.watching.trello_db import DataFormatter, Database, VersionDetector

console = logging.root.getChild("console")


def filter_cards(cards: typing.Iterable[Card], ns: argparse.Namespace):
    for card in cards:
        if DataLabels.BOOKS in card.labels:
            continue

        if ns.by_title or ns.by_url:
            if card.title != ns.by_title and card.source_url != ns.by_url:
                continue
        else:
            if DataLabels.COMPLETED in card.labels:
                if not ns.ended:
                    continue
            else:
                if not ns.ongoing:
                    continue

        yield card


def filter_pending_cards(cards: typing.Iterable[PendingCard], ns: argparse.Namespace,
                         scrapper_guesser: ScrapperGuesser):
    for pending_card in cards:
        if DataLabels.BOOKS in pending_card.labels:
            continue

        try:
            url, scrapper = scrapper_guesser.get_for_pending(pending_card)
        except NoScrapperAvailableException:
            console.warning(f"No scrapper found for {pending_card.name}, skipping")
            continue

        if ns.by_url and ns.by_url != url:
            continue

        yield pending_card, url, scrapper


def main():
    p = argparse.ArgumentParser(
        description="Track your shows.", epilog=textwrap.dedent(
            """\
                Available environment variables: .
                
                Config file is searched for in following order:
                  - config path provided in commandline
                  - CONFIG_FILE env
                  - gwatching.yaml in current directory
                  - ~/.config/gwatching.yaml
                  - lastly, configuration is created from env vars: BOARD_ID, APP_KEY, APP_SECRET, USER_KEY, USER_SECRET
                
                To create config and register user token you can run: python -m glorpen.watching.config --help
                """
        ), formatter_class=argparse.RawTextHelpFormatter
    )
    p.add_argument("--setup", action="store_true", default=False, help="Clean labels and provision board")
    p.add_argument("--pending", action="store_true", default=False, help="Process pending cards")
    p.add_argument("--ongoing", action="store_true", default=False, help="Process ongoing cards")
    p.add_argument("--ended", action="store_true", default=False, help="Process aired/ended cards")
    p.add_argument("--by-title", metavar="TITLE", default=None, help="Process cards with given title")
    p.add_argument("--by-url", metavar="URL", default=None, help="Process card with given source url")
    p.add_argument("--config", metavar="PATH", default=None, help="Path to config file")
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity")
    p.add_argument("-q", "--quiet", action="count", default=0, help="Decrease verbosity")

    ns = p.parse_args()

    log_levels = [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL
    ]

    handler = logging.StreamHandler(DummyTqdmFile(sys.stdout))
    handler.setFormatter(logging.Formatter("%(message)s"))
    console.addHandler(handler)
    console.propagate = False

    log_level = log_levels[max(min(2 - ns.verbose + ns.quiet, len(log_levels) - 1), 0)]
    logging.basicConfig(level=log_level)

    config = load_config(ns.config)
    scrapper_guesser = ScrapperGuesser()

    db = Database(
        config.app_key, config.app_secret,
        version_detector=VersionDetector(),
        data_formatter=DataFormatter()
    )
    db.login(
        config.user_key, config.user_secret,
        config.board_id
    )

    if ns.setup:
        console.info("Fixing labels")
        db.fix_labels()
        console.info("Provisioning board")
        db.setup()

    with logging_redirect_tqdm():
        if ns.ongoing or ns.ended or ns.by_title or ns.by_url:
            cards = list(filter_cards(db.cards, ns))
            for card in tqdm.tqdm(cards):
                console.info(f"Checking {card.title} / {card.id}")
                scrapper = scrapper_guesser.get_for_url(card.source_url)
                data = scrapper.get(card.source_url)
                db.save(card, data)

        if ns.pending or ns.by_url:
            cards = list(filter_pending_cards(db.cards.get_pending(), ns, scrapper_guesser))
            for pending_card, url, scrapper in tqdm.tqdm(cards):
                if db.cards.has_source_url(url):
                    console.warning(f"Duplicated url, skipping {pending_card.name}")
                    continue

                data = scrapper.get(url)
                db.save_pending(pending_card, data)


if __name__ == "__main__":
    main()
