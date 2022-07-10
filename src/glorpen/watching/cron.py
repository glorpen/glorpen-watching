import argparse
import logging
import os
import time

import more_itertools
import pyparsing
import schedule

from glorpen.watching import app

console = logging.root.getChild("glorpen.watching.cron")

time_parts = pyparsing.Or(
    [pyparsing.Literal(part) for part in
     more_itertools.flatten((range, f"{range}s") for range in ("hour", "day", "week", "minute", "second"))
     ]
)

specific_time = pyparsing.Literal("at") + pyparsing.Regex(r':?\d+(:\d+)*')

command = pyparsing.Literal(
    "do"
) + pyparsing.OneOrMore(
    pyparsing.Or(
        [pyparsing.Regex(r"-[vq]+"), *(pyparsing.Literal(f"--{option}") for option in
                                       ("pending", "completed", "setup", "ongoing", "verbose", "quiet", "config"))]
    )
)

grammar = pyparsing.Literal("every") + pyparsing.Optional(
    pyparsing.Word(pyparsing.nums), default=1
) + time_parts + pyparsing.Optional(specific_time) + command


def run_job(name, args):
    console.info(f"Starting job {name}")
    app.main(args)
    console.info(f"Job {name} completed")


def schedule_job(name: str, config_line: str):
    tokens = grammar.parse_string(config_line, parse_all=True)

    pos = 1
    job: schedule.Job = getattr(schedule.every(tokens[pos]), tokens[pos + 1])
    pos += 2
    if tokens[3] == 'at':
        job = getattr(job, 'at')(tokens[4])
        pos += 2

    args = tokens[pos + 1:]
    job.do(run_job, name=name, args=args)

    console.info(f"Registered {name} to run on {job.next_run}")


if __name__ == "__main__":

    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity")
    p.add_argument("-q", "--quiet", action="count", default=0, help="Decrease verbosity")

    ns = p.parse_args()

    log_level = app.get_log_level(ns.verbose, ns.quiet)
    logging.getLogger("schedule").setLevel(log_level)
    console.setLevel(log_level)
    logging.basicConfig(level=log_level)

    for k, v in os.environ.items():
        if k.startswith("JOB_"):
            schedule_job(k[4:], v)

    while True:
        schedule.run_pending()
        time.sleep(1)
