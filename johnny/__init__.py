# SPDX-FileCopyrightText: 2020 Adfinis-SyGroup
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
import re
import sys
from itertools import groupby
from urllib.parse import urlparse

import aiohttp
import click
import toml
from packaging import version

parallelism = 100
github_base = "https://api.github.com/repos"
gitlab_base = "https://gitlab.com"
arch_base = "https://www.archlinux.org/packages/search/json"
aur_base = "https://aur.archlinux.org/rpc"

asession = aiohttp.ClientSession()
tag_match = re.compile(r"^[0-9a-fA-F]+\s+refs/tags/([^/^]+)(\^\{\})?$")

fetch_sem = asyncio.Semaphore(value=parallelism)


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def try_parse_versions(versions):
    res = []
    for ver in versions:
        ver = ver.strip("v")
        ver = version.parse(ver)
        if not isinstance(ver, version.LegacyVersion):
            ver = version.parse(ver.base_version)
            res.append(ver)
    return sorted(res)


async def fetch(name, url, headers=None):
    async with fetch_sem:
        r = await asession.get(url, headers=headers)
        r = await r.text()
    return (name, r)


def git_get_version(line):
    m = tag_match.match(line)
    if m:
        return m.group(1)
    return None


async def git(args, pkgs):
    res = {}
    aws = []
    for name, pkg in pkgs.items():
        primary = pkg.get("primary")
        base = pkg.get("url")
        if base and primary == "git":
            u = urlparse(base)
            if u.scheme in ("http", "https"):
                aws.append(fetch(name, f"{base}/info/refs?service=git-upload-pack"))
    if not aws:
        return {}
    done, _ = await asyncio.wait(aws)
    for t in done:
        vers = set()
        name, r = t.result()
        for line in r.splitlines():
            tag = git_get_version(line)
            if tag:
                vers.add(tag)
        vers = try_parse_versions(vers)
        if vers:
            res[name] = vers[-1]
    return res
    # TODO reenable call to git
    # out = check_output(["git", "ls-remote", "--tags", base]).decode("UTF-8")


async def gitlab(args, pkgs, type="releases", field="tag_name"):
    res = {}
    aws = []
    arg_gitlab_token = args["gitlab_token"]
    for name, pkg in pkgs.items():
        id_ = pkg.get("gitlab")
        if id_:
            id_ = id_.replace("/", "%2F")
            base = pkg.get("url", gitlab_base)
            headers = {}
            if arg_gitlab_token and base == github_base:
                headers = {"Private-Token": f"token {arg_gitlab_token}"}
            aws.append(
                fetch(name, f"{base}/api/v4/projects/{id_}/{type}", headers=headers)
            )
    if not aws:
        return {}
    done, _ = await asyncio.wait(aws)
    for t in done:
        name, r = t.result()
        j = json.loads(r)
        if j:
            vers = [x[field] for x in j if field in x]
            vers = try_parse_versions(vers)
            if vers:
                res[name] = vers[-1]
    return res


async def gitlab_tags(args, pkgs):
    return await gitlab(args, pkgs, "repository/tags", "name")


async def github(args, pkgs, type="releases", field="tag_name"):
    res = {}
    aws = []
    arg_github_token = args["github_token"]
    for name, pkg in pkgs.items():
        id_ = pkg.get("github")
        if id_:
            headers = None
            if arg_github_token:
                headers = {"Authorization": f"token {arg_github_token}"}
            aws.append(fetch(name, f"{github_base}/{id_}/{type}", headers=headers))
    if not aws:
        return {}
    done, _ = await asyncio.wait(aws)
    for t in done:
        name, r = t.result()
        j = json.loads(r)
        if j:
            vers = [x[field] for x in j if field in x]
            vers = try_parse_versions(vers)
            if vers:
                res[name] = vers[-1]
    return res


async def github_tags(args, pkgs):
    return await github(args, pkgs, "tags", "name")


async def arch(args, pkgs):
    res = {}
    aws = []
    for name, pkg in pkgs.items():
        id_ = pkg.get("arch", name)
        r = aws.append(fetch(name, f"{arch_base}/?name={id_}"))
    if not aws:
        return {}
    done, _ = await asyncio.wait(aws)
    for t in done:
        name, r = t.result()
        j = json.loads(r)
        j = j["results"]
        if j:
            vers = try_parse_versions([j[0]["pkgver"]])
            if vers:
                res[name] = vers[0]
    return res


async def aur(args, pkgs):
    query = []
    items = list(pkgs.items())
    for name, pkg in items:
        id_ = pkg.get("aur", name)
        query.append(f"arg[]={id_}")
    query = "&".join(query)
    _, r = await fetch("aur", f"{aur_base}/?v=5&type=info&{query}")
    j = json.loads(r)
    j = j["results"]
    res = {}
    for i, v in enumerate(j):
        if v:
            vers = try_parse_versions([v["Version"]])
            if vers:
                res[items[i][0]] = vers[0]
    return res


sources_list = [github, gitlab, aur, arch]
sources = {x.__name__: x for x in sources_list}
sources["github_tags"] = github_tags
sources["gitlab_tags"] = gitlab_tags
sources["git"] = git


def update(old, new):
    res = dict(old)
    for k, v in new.items():
        if k not in res:
            res[k] = v
        else:
            ov = res[k]
            if v > ov:
                res[k] = v
    return res


def make_serializable(s):
    return {k: str(v) for k, v in s.items()}


def status(args, source, query, new, all):
    if args["quiet"]:
        return
    if args["print_names"]:
        squery = ", ".join(query)
        eprint(f"Asking {source} for:\n    {squery}")
        if new:
            snew = ", ".join(new.keys())
            eprint(f"found:\n    {snew}, total: {all}\n")
        else:
            eprint(f"total: {all}\n")
    else:
        eprint(
            f"Asking {source} for {len(query)} packages, found {len(new)}, total: {all}"
        )


async def do_get_primary(s, args, x, k):
    return (args, x, k, await s(args, x))


async def get_primary(args, c, vers):
    primary = [(k, v) for k, v in c.items() if "primary" in v]
    primary = sorted(primary, key=lambda i: i[1]["primary"])
    primary = groupby(primary, key=lambda i: i[1]["primary"])
    vers = dict(vers)
    asked = set()
    aws = []
    for k, g in primary:
        g = list(g)
        x = dict(g)
        asked.add(k)
        s = sources[k]
        aws.append(do_get_primary(s, args, x, k))
    if not aws:
        return vers, asked
    done, _ = await asyncio.wait(aws)
    for t in done:
        args, x, k, new = t.result()
        vers = update(vers, new)
        status(args, k, x, new, len(vers))
    if not args["quiet"]:
        eprint("primary done")
    return vers, asked


async def get_secondary_source(args, c, s, vers, left):
    new = await s(args, left)
    vers = update(vers, new)
    status(args, s.__name__, left, new, len(vers))
    arg_trust_secondary = args["trust_secondary"]
    if arg_trust_secondary:
        return vers, {k: v for k, v in c.items() if k not in vers}
    return vers, left


async def run_secondary(args, c, vers, asked, left, l):
    if left:
        for s in [x for x in sources_list if l(x.__name__, asked)]:
            vers, left = await get_secondary_source(args, c, s, vers, left)
            if not left:
                break
    return vers, left


async def get_secondary(args, c, vers, asked, left):
    vers = dict(vers)
    # Do not ask the sources we just asked (a slight optimization)
    vers, left = await run_secondary(
        args, c, vers, asked, left, lambda name, asked: name not in asked
    )
    vers, left = await run_secondary(
        args, c, vers, asked, left, lambda name, asked: name in asked
    )
    return vers, left


async def get_vers(args, c):
    try:
        arg_primary = args["primary"]
        arg_secondary = args["secondary"]
        arg_trust_primary = args["trust_primary"]
        vers = {}
        asked = set()
        if arg_primary:
            vers, asked = await get_primary(args, c, vers)
        if arg_trust_primary:
            left = {k: v for k, v in c.items() if k not in vers}
        else:
            left = dict(c)
        if arg_secondary and left:
            vers, left = await get_secondary(args, c, vers, asked, left)
        left = ", ".join([k for k in left.keys()])
        if left:
            eprint(f"Packages left: {left}")
        return vers, left
    finally:
        await asession.close()


defaults = {
    "primary": True,
    "secondary": True,
    "trust_primary": True,
    "trust_secondary": True,
    "print_names": False,
    "quiet": False,
}


def read_config(args, config):
    args = dict(args)
    for k in config.keys():
        if k not in args:
            raise KeyError(k, "Unknown config option")
    for k, v in args.items():
        if v is None:
            args[k] = config.get(k, defaults.get(k))
    return args


@click.command(
    help=(
        "johnny - generic dep(p)endencies tracker\n\n"
        "command-line options take precedence over config options.\n\n"
        "tokens are only needed for high rate queries. (rate-limit)"
    )
)
@click.argument("config", type=click.File("r", encoding="UTF-8"))
@click.option("--github-token", type=click.STRING, help="github token")
@click.option("--gitlab-token", type=click.STRING, help="gitlab token")
@click.option("--primary/--no-primary", default=None, help="query primary sources")
@click.option("--secondary/--no-secondary", default=None, help="query primary sources")
@click.option(
    "--trust-primary/--no-trust-primary", default=None, help="trust primary sources"
)
@click.option(
    "--trust-secondary/--no-trust-secondary",
    default=None,
    help="trust secondary sources",
)
@click.option(
    "--print-names/--no-print-names",
    default=None,
    help="print package names instead of count",
)
@click.option(
    "--quiet/--no-quiet", default=None, help="do not print anything to stderr",
)
def cli(config, **kwargs):
    c = toml.load(config)
    jc = c.get("johnny_config", {})
    kwargs = read_config(kwargs, jc)
    if jc:
        del c["johnny_config"]
    loop = asyncio.get_event_loop()
    vers, left = loop.run_until_complete(get_vers(kwargs, c))
    print(json.dumps(make_serializable((vers))))
    if left:
        sys.exit(1)


if __name__ == "__main__":
    cli()
