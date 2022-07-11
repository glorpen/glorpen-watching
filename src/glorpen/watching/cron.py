import argparse
import itertools
import logging
import os
import textwrap
import time

import more_itertools
import pyparsing
import schedule

from glorpen.watching import app

console = logging.root.getChild("glorpen.watching.cron")

weekdays = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
time_units = tuple(
    more_itertools.flatten((range, f"{range}s") for range in ("hour", "day", "week", "minute", "second"))
)

specific_time = pyparsing.Literal("at") + pyparsing.Regex(r':?\d+(:\d+)*')

command = pyparsing.Literal(
    "do"
) + pyparsing.OneOrMore(
    pyparsing.Or(
        [pyparsing.Regex(r"-[vq]+")] + [pyparsing.Literal(f"--{option}") for option in
                                        ("pending", "completed", "setup", "ongoing", "verbose", "quiet", "config")]
    )
)

grammar = pyparsing.Literal("every") + pyparsing.Optional(
    pyparsing.Or(itertools.chain([pyparsing.Word(pyparsing.nums)], map(pyparsing.Literal, weekdays))), default=1
) + pyparsing.Or(map(pyparsing.Literal, time_units)) + pyparsing.Optional(specific_time) + command


def run_job(name, args):
    console.info(f"Starting job {name}")
    app.main(args)
    console.info(f"Job {name} completed")


def schedule_job(name: str, config_line: str):
    tokens = list(reversed(grammar.parse_string(config_line, parse_all=True)))
    job: schedule.Job

    assert tokens.pop() == "every"

    token = tokens.pop()
    if token in weekdays:
        job = getattr(schedule.every(), token)
    else:
        interval = int(token)
        token = tokens.pop()
        assert token in time_units
        job = getattr(schedule.every(interval), token)

    token = tokens.pop()
    if token == "at":
        job = job.at(tokens.pop())
        token = tokens.pop()

    assert token == "do"
    job.do(run_job, name=name, args=tokens)

    console.info(f"Registered {name} to run on {job.next_run}")


if __name__ == "__main__":

    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=textwrap.dedent(
            """\
                To schedule gwatching runs, define some env variables, for eg.:
                JOB_hourly="every 1 hour at 00:34 do --pending -qqq"
                JOB_weekly1="every 1 week at 12:00:00 do --pending -qqq"
                JOB_weekly2="every monday at 12:00:00 do --pending -qqq"
                
                Supported grammar:
                every [number] <second|minute|hour|day|week|seconds|minutes|...> [at <[[HH:]MM]:SS>] do <command>
                every <day name> [at <[[HH:]MM]:SS>] do <command>
                """
        )
    )
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
